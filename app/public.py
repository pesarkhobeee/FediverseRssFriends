"""Public radar page: /u/{handle} + OPML export + visibility settings.

Visibility rules (single dependency, used everywhere a user's radar leaks):
- public   → anyone can view
- unlisted → anyone with the URL can view (not listed on /discover, step 12)
- private  → only the owner (follow-based access lands in step 8)
"""

import re
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlmodel import Session, select

from . import activity, config
from .auth import current_user, login_required
from .db import (
    Activity,
    Bookmark,
    Category,
    Feed,
    FeedItem,
    Follow,
    HandleAlias,
    Subscription,
    User,
    get_session,
)
from .importers import add_subscription_by_feed, fork_from
from .templating import make_templates

NEW_BADGE_WINDOW = timedelta(hours=24)

templates = make_templates()

router = APIRouter()

RECENT_ITEMS_PER_FEED = 5


def resolve_handle(db: Session, handle: str) -> User | None:
    """Return the User for a handle, treating retired handles as aliases.

    Looks up:
      1. Live `User.handle` (case-sensitive — handles are lowercase throughout).
      2. `HandleAlias.old_handle` → resolves to the current owner of that alias.

    Returns None if the handle is neither live nor a retired alias.
    Callers that render GET responses should follow up with
    `maybe_redirect_to_canonical` so old-handle URLs 301 to current.
    """
    if not handle:
        return None
    user = db.exec(select(User).where(User.handle == handle)).first()
    if user:
        return user
    alias = db.exec(select(HandleAlias).where(HandleAlias.old_handle == handle)).first()
    if alias:
        return db.get(User, alias.user_id)
    return None


def maybe_redirect_to_canonical(
    request: Request, requested_handle: str, owner: User,
) -> RedirectResponse | None:
    """If the URL path used a retired alias, 301 to the canonical path.

    - Only redirects on GET (POST/DELETE/PUT: serve normally under the
      alias, since some HTTP clients / RSS libs don't preserve method
      across 301).
    - Preserves the rest of the path (`.rss`, `/bookmarks`,
      `/c/News.rss`, `/activity/N`, etc.) and any query string.
    """
    if requested_handle == owner.handle:
        return None
    if request.method != "GET":
        return None
    old_prefix = f"/u/{requested_handle}"
    new_prefix = f"/u/{owner.handle}"
    new_path = request.url.path.replace(old_prefix, new_prefix, 1)
    if request.url.query:
        new_path = f"{new_path}?{request.url.query}"
    return RedirectResponse(new_path, status_code=301)


def can_view(viewer: User | None, target: User, db: Session | None = None) -> bool:
    if target.visibility in ("public", "unlisted"):
        return True
    if viewer and viewer.id == target.id:
        return True
    # Private: an approved follower can view.
    if viewer and db is not None:
        row = db.get(Follow, (viewer.id, target.id))
        if row and row.approved_at is not None:
            return True
    return False


def _load_public_radar(
    db: Session, owner: User, category_filter: str | None, viewer: User | None = None
) -> dict:
    """Categories → subs → feed → recent items. Filters by category name if given."""
    categories = db.exec(
        select(Category)
        .where(Category.user_id == owner.id)
        .order_by(Category.sort_order, Category.id)
    ).all()
    if category_filter:
        categories = [c for c in categories if c.name.lower() == category_filter.lower()]

    cat_ids = [c.id for c in categories]
    subs_rows = []
    if cat_ids:
        subs_rows = db.exec(
            select(Subscription, Feed)
            .join(Feed, Feed.id == Subscription.feed_id)
            .where(Subscription.user_id == owner.id)
            .where(Subscription.category_id.in_(cat_ids))
            .order_by(Subscription.sort_order, Subscription.id)
        ).all()

    feed_ids = {feed.id for _s, feed in subs_rows}
    items_by_feed: dict[int, list[FeedItem]] = {}
    if feed_ids:
        # Simple approach: one query per feed. Fine at MVP scale (dozens of feeds).
        for fid in feed_ids:
            items_by_feed[fid] = db.exec(
                select(FeedItem)
                .where(FeedItem.feed_id == fid)
                .order_by(FeedItem.published_at.desc().nulls_last(), FeedItem.fetched_at.desc())
                .limit(RECENT_ITEMS_PER_FEED)
            ).all()

    now = datetime.now(UTC)

    def _is_new(entries: list[FeedItem]) -> bool:
        if not entries or not entries[0].published_at:
            return False
        pub = entries[0].published_at
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=UTC)
        return (now - pub) < NEW_BADGE_WINDOW

    def _sort_key(item_list: list[FeedItem]) -> datetime:
        """Newest-item published_at (fallback: fetched_at). Missing → very old."""
        if not item_list:
            return datetime.min.replace(tzinfo=UTC)
        top = item_list[0]
        dt = top.published_at or top.fetched_at
        if dt is None:
            return datetime.min.replace(tzinfo=UTC)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    by_cat: dict[int, list[dict]] = {c.id: [] for c in categories}
    for sub, feed in subs_rows:
        entries = items_by_feed.get(feed.id, [])
        by_cat.setdefault(sub.category_id, []).append(
            {"sub": sub, "feed": feed, "entries": entries, "is_new": _is_new(entries)}
        )
    # Sort each category by newest content first, matching lovelyRSS.
    for cat_id in by_cat:
        by_cat[cat_id].sort(key=lambda x: _sort_key(x["entries"]), reverse=True)

    all_categories = db.exec(
        select(Category)
        .where(Category.user_id == owner.id)
        .order_by(Category.sort_order, Category.id)
    ).all()

    total_feeds = db.exec(
        select(Subscription).where(Subscription.user_id == owner.id)
    ).all()

    viewer_bookmarks: set[int] = set()
    viewer_subs: set[int] = set()
    if viewer:
        all_item_ids = [
            item.id for items in items_by_feed.values() for item in items
        ]
        if all_item_ids:
            viewer_bookmarks = set(
                db.exec(
                    select(Bookmark.feed_item_id)
                    .where(Bookmark.user_id == viewer.id)
                    .where(Bookmark.feed_item_id.in_(all_item_ids))
                ).all()
            )
        # Which of the shown feeds the viewer already has on their own
        # radar — drives the CTA state so a "Following" pill replaces
        # the button on rows they've already added.
        shown_feed_ids = [f.id for _s, f in subs_rows]
        if shown_feed_ids:
            viewer_subs = set(
                db.exec(
                    select(Subscription.feed_id)
                    .where(Subscription.user_id == viewer.id)
                    .where(Subscription.feed_id.in_(shown_feed_ids))
                ).all()
            )

    last_fetched = [f.last_fetched_at for _s, f in subs_rows if f.last_fetched_at]
    last_updated = max(last_fetched) if last_fetched else None
    if last_updated and last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=UTC)

    # Flat view: when no category filter is active, every feed goes into
    # one list sorted by newest item across the entire radar. Matches
    # lovelyRSS's default "All Feeds" behavior.
    flat_subs: list[dict] = []
    if not category_filter:
        cat_name_by_id = {c.id: c.name for c in categories}
        for sub, feed in subs_rows:
            entries = items_by_feed.get(feed.id, [])
            flat_subs.append({
                "sub": sub,
                "feed": feed,
                "entries": entries,
                "is_new": _is_new(entries),
                "category_name": cat_name_by_id.get(sub.category_id, ""),
            })
        flat_subs.sort(key=lambda x: _sort_key(x["entries"]), reverse=True)

    # Which of the OWNER's categories does the viewer already
    # follow at the category level? Drives the follow-button state on
    # the per-category chip UI. Empty dict for anon/owner.
    category_follow_states: dict[int, str] = {}
    if viewer and viewer.id != owner.id and all_categories:
        from .db import CategoryFollow
        followed_cat_ids = set(db.exec(
            select(CategoryFollow.category_id)
            .where(CategoryFollow.follower_id == viewer.id)
            .where(CategoryFollow.category_id.in_([c.id for c in all_categories]))
        ).all())
        category_follow_states = {
            c.id: ("following" if c.id in followed_cat_ids else "none")
            for c in all_categories
        }

    active_category = None
    if category_filter:
        for c in all_categories:
            if c.name.lower() == category_filter.lower():
                active_category = c
                break

    return {
        "owner": owner,
        "categories_view": [{"category": c, "subs": by_cat.get(c.id, [])} for c in categories],
        "flat_subs": flat_subs,
        "all_categories": all_categories,
        "active_cat": category_filter or "",
        "active_category": active_category,
        "category_follow_states": category_follow_states,
        "total_feeds": len(total_feeds),
        "viewer_bookmarks": viewer_bookmarks,
        "viewer_subs": viewer_subs,
        "last_updated": last_updated,
        "refresh_interval_minutes": config.REFRESH_INTERVAL_MINUTES,
    }


@router.get("/u/{handle}.rss", response_class=PlainTextResponse)
def user_reading_rss(
    handle: str,
    request: Request,
    as_: str | None = Query(None, alias="as"),
    t: str | None = None,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    """The union of items across a user's subscribed feeds, newest first.

    Lets anyone drop this URL into a normal RSS reader and follow the
    person, not each individual feed. Gate rules mirror activity.rss:
    public/unlisted open; private + owner-or-approved-follower session
    allowed; private + ?as=&t= signed-token allowed while the Follow
    row remains approved.
    """
    owner = resolve_handle(db, handle)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    redirect = maybe_redirect_to_canonical(request, handle, owner)
    if redirect is not None:
        return redirect

    allowed = False
    if owner.visibility != "private":  # noqa: SIM114 — branches are semantically distinct
        allowed = True
    elif viewer and can_view(viewer, owner, db=db):
        allowed = True
    elif as_ and t:
        follower = db.exec(select(User).where(User.handle == as_)).first()
        if follower and activity.verify_follower_token(owner, follower, t):
            row = db.get(Follow, (follower.id, owner.id))
            if row and row.approved_at is not None:
                allowed = True

    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this reading feed is private")

    # Union of items across every subscribed feed, newest first.
    items = db.exec(
        select(FeedItem, Feed)
        .join(Feed, Feed.id == FeedItem.feed_id)
        .join(Subscription, Subscription.feed_id == Feed.id)
        .where(Subscription.user_id == owner.id)
        .order_by(FeedItem.published_at.desc().nulls_last(), FeedItem.fetched_at.desc())
        .limit(100)
    ).all()

    base = str(request.base_url).rstrip("/")

    def _rfc822(dt: datetime | None) -> str:
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    entries = []
    for item, feed in items:
        pubdate = _rfc822(item.published_at or item.fetched_at)
        title_prefix = f"[{feed.title}] " if feed.title else ""
        entries.append(
            "    <item>\n"
            f"      <title>{xml_escape(title_prefix + (item.title or '(untitled)'))}</title>\n"
            f"      <link>{xml_escape(item.link or feed.url)}</link>\n"
            f"      <guid isPermaLink=\"false\">item-{item.id}</guid>\n"
            f"      <source url=\"{xml_escape(feed.url)}\">{xml_escape(feed.title)}</source>\n"
            + (f"      <pubDate>{pubdate}</pubDate>\n" if pubdate else "")
            + (
                f"      <description>{xml_escape(item.summary)}</description>\n"
                if item.summary else ""
            )
            + "    </item>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>@{xml_escape(owner.handle)}'s reading list</title>\n"
        f"    <link>{base}/u/{xml_escape(owner.handle)}</link>\n"
        f"    <description>Every item across every feed "
        f"@{xml_escape(owner.handle)} subscribes to, newest first.</description>\n"
        + "\n".join(entries) + "\n"
        "  </channel>\n"
        "</rss>\n"
    )
    return PlainTextResponse(body, media_type="application/rss+xml; charset=utf-8")


@router.get("/u/{handle}/c/{cat_name}.rss", response_class=PlainTextResponse)
def user_category_rss(
    handle: str,
    cat_name: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Items scoped to a single category on this user's radar. The
    "every category is an RSS feed" surface — subscribe in any RSS
    reader to get just that curatorial slice, not the whole radar."""
    owner = resolve_handle(db, handle)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    redirect = maybe_redirect_to_canonical(request, handle, owner)
    if redirect is not None:
        return redirect
    if not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this radar is private")

    cat = db.exec(
        select(Category)
        .where(Category.user_id == owner.id)
        .where(Category.name == cat_name)
    ).first()
    if not cat:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")

    items = db.exec(
        select(FeedItem, Feed)
        .join(Feed, Feed.id == FeedItem.feed_id)
        .join(Subscription, Subscription.feed_id == Feed.id)
        .where(Subscription.user_id == owner.id)
        .where(Subscription.category_id == cat.id)
        .order_by(FeedItem.published_at.desc().nulls_last(), FeedItem.fetched_at.desc())
        .limit(100)
    ).all()

    base = str(request.base_url).rstrip("/")

    def _rfc822(dt: datetime | None) -> str:
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    entries = []
    for item, feed in items:
        pubdate = _rfc822(item.published_at or item.fetched_at)
        title_prefix = f"[{feed.title}] " if feed.title else ""
        entries.append(
            "    <item>\n"
            f"      <title>{xml_escape(title_prefix + (item.title or '(untitled)'))}</title>\n"
            f"      <link>{xml_escape(item.link or feed.url)}</link>\n"
            f"      <guid isPermaLink=\"false\">item-{item.id}</guid>\n"
            f"      <source url=\"{xml_escape(feed.url)}\">{xml_escape(feed.title)}</source>\n"
            + (f"      <pubDate>{pubdate}</pubDate>\n" if pubdate else "")
            + (
                f"      <description>{xml_escape(item.summary)}</description>\n"
                if item.summary else ""
            )
            + "    </item>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>@{xml_escape(owner.handle)}'s {xml_escape(cat.name)} feeds</title>\n"
        f"    <link>{base}/u/{xml_escape(owner.handle)}?cat={xml_escape(cat.name)}</link>\n"
        f"    <description>Items from @{xml_escape(owner.handle)}'s "
        f"'{xml_escape(cat.name)}' category, newest first.</description>\n"
        + "\n".join(entries) + "\n"
        "  </channel>\n"
        "</rss>\n"
    )
    return PlainTextResponse(body, media_type="application/rss+xml; charset=utf-8")


@router.get("/u/{handle}.opml", response_class=PlainTextResponse)
def user_opml(
    handle: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    owner = resolve_handle(db, handle)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    redirect = maybe_redirect_to_canonical(request, handle, owner)
    if redirect is not None:
        return redirect
    if not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this radar is private")

    categories = db.exec(
        select(Category)
        .where(Category.user_id == owner.id)
        .order_by(Category.sort_order, Category.id)
    ).all()
    subs = db.exec(
        select(Subscription, Feed)
        .join(Feed, Feed.id == Subscription.feed_id)
        .where(Subscription.user_id == owner.id)
    ).all()
    by_cat: dict[int, list[Feed]] = {c.id: [] for c in categories}
    for sub, feed in subs:
        by_cat.setdefault(sub.category_id, []).append(feed)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>@{xml_escape(owner.handle)}'s rssFriends</title>",
        "  </head>",
        "  <body>",
    ]
    for c in categories:
        lines.append(f'    <outline text="{xml_escape(c.name)}" title="{xml_escape(c.name)}">')
        for feed in by_cat.get(c.id, []):
            lines.append(
                '      <outline type="rss" '
                f'text="{xml_escape(feed.title)}" '
                f'title="{xml_escape(feed.title)}" '
                f'xmlUrl="{xml_escape(feed.url)}" '
                f'htmlUrl="{xml_escape(feed.site_url or "")}"/>'
            )
        lines.append("    </outline>")
    lines += ["  </body>", "</opml>"]
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/x-opml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{owner.handle}.opml"'},
    )


STREAM_PAGE_SIZE = 100  # items per page in the merged newest-first stream


def _load_stream(
    db: Session,
    owner: User,
    category_filter: str | None,
    limit: int = STREAM_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[dict], bool]:
    """Merged newest-first item stream across all of owner's subscriptions.

    Returns (rows, has_more). Each row = {item, feed, category_name}.
    `has_more` says whether a subsequent page exists (peek limit+1)."""
    # Resolve the set of subscription rows in scope (optionally filtered
    # by category name). We need the category row to render the chip.
    sub_query = (
        select(Subscription, Feed, Category)
        .join(Feed, Feed.id == Subscription.feed_id)
        .join(Category, Category.id == Subscription.category_id)
        .where(Subscription.user_id == owner.id)
    )
    if category_filter:
        sub_query = sub_query.where(
            Category.name.ilike(category_filter)  # case-insensitive match
        )
    subs = db.exec(sub_query).all()
    if not subs:
        return [], False

    feed_ids = {feed.id for _s, feed, _c in subs}
    feed_by_id = {feed.id: feed for _s, feed, _c in subs}
    cat_by_feed = {feed.id: cat.name for _s, feed, cat in subs}
    # 0.11.3: expose the owner's private note per feed so the stream
    # rows can render "@dhh — creator of Rails" on the owner's own view.
    note_by_feed = {feed.id: sub.note for sub, feed, _c in subs}

    # Peek limit+1 so we know if there's another page without a count query.
    items = db.exec(
        select(FeedItem)
        .where(FeedItem.feed_id.in_(feed_ids))
        .order_by(
            FeedItem.published_at.desc().nulls_last(),
            FeedItem.fetched_at.desc(),
        )
        .offset(offset)
        .limit(limit + 1)
    ).all()

    has_more = len(items) > limit
    items = items[:limit]

    rows = [
        {
            "item": it,
            "feed": feed_by_id[it.feed_id],
            "category_name": cat_by_feed.get(it.feed_id, ""),
            "note": note_by_feed.get(it.feed_id, ""),
        }
        for it in items
    ]
    return rows, has_more


@router.get("/u/{handle}", response_class=HTMLResponse)
def user_radar(
    handle: str,
    request: Request,
    cat: str | None = None,
    view: str = "feeds",
    offset: int = 0,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    owner = resolve_handle(db, handle)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    redirect = maybe_redirect_to_canonical(request, handle, owner)
    if redirect is not None:
        return redirect

    from .follow import follow_state, follower_count, following_count

    def _signed_url(base_path: str) -> str:
        """Return a URL to `base_path`, appending a signed follower token when
        the viewer is an approved follower of a private owner."""
        if owner.visibility == "private" and viewer and viewer.id != owner.id:
            token = activity.sign_follower(owner, viewer)
            return f"{base_path}?as={viewer.handle}&t={token}"
        return base_path

    if not can_view(viewer, owner, db=db):
        # Private radar — render a friendly locked page with a Follow button
        # (creates a pending request) instead of a machine-readable 403.
        return templates.TemplateResponse(
            request,
            "user_radar_locked.html",
            {
                "owner": owner,
                "user": viewer,
                "follow_state": follow_state(db, viewer.id, owner.id) if viewer else "none",
                "followers_n": follower_count(db, owner.id),
                "following_n": following_count(db, owner.id),
            },
            status_code=403,
        )

    view = view if view in ("feeds", "stream") else "feeds"

    # HTMX "Load more" in the stream view targets ONLY the item list —
    # not the whole page. When the request comes in with HX-Request=true
    # and the "streampage" trigger set, return just the partial.
    if view == "stream" and request.headers.get("HX-Request") == "true":
        if not can_view(viewer, owner, db=db):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "this radar is private")
        stream_rows, has_more = _load_stream(db, owner, cat, offset=max(0, offset))
        viewer_bookmarks: set[int] = set()
        if viewer and stream_rows:
            item_ids = [r["item"].id for r in stream_rows]
            viewer_bookmarks = set(db.exec(
                select(Bookmark.feed_item_id)
                .where(Bookmark.user_id == viewer.id)
                .where(Bookmark.feed_item_id.in_(item_ids))
            ).all())
        return templates.TemplateResponse(
            request,
            "_stream_page.html",
            {
                "owner": owner,
                "user": viewer,
                "stream_rows": stream_rows,
                "stream_has_more": has_more,
                "stream_next_offset": offset + STREAM_PAGE_SIZE,
                "viewer_bookmarks": viewer_bookmarks,
                "active_cat": cat,
                "is_owner": bool(viewer and viewer.id == owner.id),
            },
        )

    ctx = _load_public_radar(db, owner, cat, viewer=viewer)
    ctx["user"] = viewer
    ctx["is_owner"] = bool(viewer and viewer.id == owner.id)
    ctx["follow_state"] = follow_state(db, viewer.id, owner.id) if viewer else "none"
    ctx["followers_n"] = follower_count(db, owner.id)
    ctx["following_n"] = following_count(db, owner.id)
    ctx["reading_rss_url"] = _signed_url(f"/u/{owner.handle}.rss")
    ctx["activity_rss_url"] = _signed_url(f"/u/{owner.handle}/activity.rss")
    ctx["view"] = view
    if view == "stream":
        stream_rows, has_more = _load_stream(db, owner, cat, offset=max(0, offset))
        ctx["stream_rows"] = stream_rows
        ctx["stream_has_more"] = has_more
        ctx["stream_next_offset"] = offset + STREAM_PAGE_SIZE
        # Bookmark state for viewer over these items.
        viewer_bookmarks = set()
        if viewer and stream_rows:
            item_ids = [r["item"].id for r in stream_rows]
            viewer_bookmarks = set(db.exec(
                select(Bookmark.feed_item_id)
                .where(Bookmark.user_id == viewer.id)
                .where(Bookmark.feed_item_id.in_(item_ids))
            ).all())
        ctx["viewer_bookmarks"] = viewer_bookmarks
    return templates.TemplateResponse(request, "user_radar.html", ctx)


@router.post("/settings/visibility")
def set_visibility(
    visibility: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    if visibility not in ("public", "unlisted", "private"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid visibility")
    user.visibility = visibility
    db.add(user)
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


# ---------- Handle rename (0.9.0) ----------

# Same regex applied at signup — lowercase a-z + digits + hyphens, must
# start with a letter, 3-32 chars. Prevents confusable/reserved-looking
# strings ('123', '--foo--') from becoming URL segments.
_HANDLE_RE = re.compile(r"^[a-z][a-z0-9-]{2,31}$")

# Path segments already claimed by app routes. Renaming into any of
# these would collide with the URL router. Keep in sync when adding
# new top-level routes.
_RESERVED_HANDLES = frozenset({
    "login", "logout", "dashboard", "feed", "discover", "help", "settings",
    "follow-requests", "u", "f", "items", "feeds", "categories",
    "subscriptions", "activity", "auth", "static", "sw.js",
    "manifest.webmanifest", "healthz", "version", "followers", "following",
    "account", "admin", "api", "search", "well-known",
})

RENAME_COOLDOWN_DAYS = 30


@router.post("/settings/handle")
def rename_handle(
    new_handle: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Rename the signed-in user's handle.

    On success:
      - Retires the old handle in HandleAlias (permanently reserved).
      - Rewrites denormalized `@old-handle` string mentions in Activity.
      - Rewrites Activity.subject_url paths containing /u/{old}/… .
      - Rewrites Feed.url rows for category-follow auto-created feeds
        that embed the handle (`rssfriends.com/u/{old}/c/{cat}.rss`).
      - Sets user.last_renamed_at for the 30-day cooldown.
    """
    from .db import HandleAlias

    new_handle = (new_handle or "").strip().lower()

    if new_handle == user.handle:
        return RedirectResponse("/dashboard?panel=settings", status_code=303)

    if not _HANDLE_RE.match(new_handle):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Handle must be 3-32 characters: lowercase letters, digits, or "
            "hyphens; starting with a letter.",
        )

    if new_handle in _RESERVED_HANDLES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{new_handle}' is a reserved name inside rssFriends. Pick another.",
        )

    now = datetime.now(UTC)
    if user.last_renamed_at is not None:
        last = user.last_renamed_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        elapsed = now - last
        if elapsed < timedelta(days=RENAME_COOLDOWN_DAYS):
            days_left = RENAME_COOLDOWN_DAYS - elapsed.days
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"You renamed recently. Try again in {days_left} day"
                f"{'' if days_left == 1 else 's'}.",
            )

    # Live-user collision.
    if db.exec(select(User).where(User.handle == new_handle)).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{new_handle}' is already taken by another user.",
        )
    # Retired-alias collision (including the user's OWN prior aliases —
    # deliberately: renaming back to a prior name would confuse the
    # redirect chain, so we forbid it. Users who typo'd get their new
    # handle stuck until cooldown expires and they rename to something
    # else again).
    if db.exec(select(HandleAlias).where(HandleAlias.old_handle == new_handle)).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{new_handle}' was used by another (or your prior) account and "
            "is permanently reserved.",
        )

    old_handle = user.handle

    # 1. Retire the old handle.
    db.add(HandleAlias(old_handle=old_handle, user_id=user.id, renamed_at=now))

    # 2. Rewrite denormalized Activity rows. Two shapes:
    #    - `subject == "@old"` (follow/fork rows) → `"@new"`
    #    - `subject_url` starting with "/u/old" or containing "/u/old/"
    #    Fetch matching rows and rewrite in Python — the dataset is
    #    small (single-user activity history) and this avoids
    #    DB-specific string-manipulation SQL.
    old_at = f"@{old_handle}"
    new_at = f"@{new_handle}"
    old_prefix = f"/u/{old_handle}"
    new_prefix = f"/u/{new_handle}"

    for row in db.exec(
        select(Activity)
        .where(Activity.user_id == user.id)  # only the renaming user's rows
    ).all():
        changed = False
        if row.subject == old_at:
            row.subject = new_at
            changed = True
        if row.subject_url and (
            row.subject_url == old_prefix or row.subject_url.startswith(old_prefix + "/")
        ):
            row.subject_url = new_prefix + row.subject_url[len(old_prefix):]
            changed = True
        if changed:
            db.add(row)
    # Also rewrite subject_url pointing at THIS user across everyone
    # else's activity (e.g. `@viewer followed @old`, where subject_url
    # is /u/old). Same rewrite rule; wider scope.
    for row in db.exec(
        select(Activity)
        .where(Activity.user_id != user.id)
        .where(Activity.ref_type == "user")
        .where(Activity.ref_id == user.id)
    ).all():
        if row.subject == old_at:
            row.subject = new_at
            db.add(row)
        if row.subject_url and (
            row.subject_url == old_prefix or row.subject_url.startswith(old_prefix + "/")
        ):
            row.subject_url = new_prefix + row.subject_url[len(old_prefix):]
            db.add(row)

    # 3. Rewrite category-follow-created Feed URLs
    #    (`rssfriends.com/u/{handle}/c/{cat}.rss`).
    old_feed_frag = f"/u/{old_handle}/c/"
    new_feed_frag = f"/u/{new_handle}/c/"
    for feed in db.exec(select(Feed).where(Feed.url.contains(old_feed_frag))).all():
        feed.url = feed.url.replace(old_feed_frag, new_feed_frag, 1)
        db.add(feed)

    # 5. Finally: swap the User's handle + stamp the cooldown anchor.
    user.handle = new_handle
    user.last_renamed_at = now
    db.add(user)

    db.commit()
    return RedirectResponse(f"/u/{new_handle}", status_code=303)


# ---------- Wallabag integration ----------


@router.post("/settings/wallabag")
def save_wallabag(
    instance_url: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Save the user's Wallabag creds and try grant_type=password to
    obtain a token pair right away. Redirects back to /dashboard with
    a flash indicating success or failure.

    Password grant is the only OAuth flow Wallabag supports — there is
    no /oauth/v2/auth authorization endpoint. See docs/deploy considerations
    and https://doc.wallabag.org/developer/api/oauth/."""
    from . import wallabag as wb
    from .db import WallabagAccount

    instance = instance_url.strip().rstrip("/")
    if not instance.startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "instance URL must start with http:// or https://",
        )

    account = wb.get_account(db, user)
    if account is None:
        account = WallabagAccount(user_id=user.id, instance_url=instance)
    account.instance_url = instance
    account.client_id = client_id.strip()
    wb.store_credentials(
        account,
        client_secret=client_secret.strip(),
        username=username.strip(),
        password=password,  # Wallabag passwords can contain spaces/etc.
    )
    account.access_token = ""
    account.refresh_token = ""
    account.token_expires_at = None
    account.enabled = False
    account.updated_at = datetime.now(UTC)
    db.add(account)
    db.commit()
    db.refresh(account)

    ok = wb.password_grant(db, account)
    return RedirectResponse(
        f"/dashboard?panel=settings&wallabag={'ok' if ok else 'err'}",
        status_code=303,
    )


@router.post("/settings/wallabag/disconnect")
def wallabag_disconnect(
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Remove the WallabagAccount row entirely."""
    from . import wallabag as wb

    account = wb.get_account(db, user)
    if account is not None:
        db.delete(account)
        db.commit()
    return RedirectResponse("/dashboard?panel=settings", status_code=303)


@router.post("/settings/wallabag/toggle")
def wallabag_toggle(
    enabled: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Enable/disable the integration without discarding tokens."""
    from . import wallabag as wb

    account = wb.get_account(db, user)
    if account is not None and account.access_token:
        account.enabled = enabled == "on"
        account.updated_at = datetime.now(UTC)
        db.add(account)
        db.commit()
    return RedirectResponse("/dashboard?panel=settings", status_code=303)


# ---------- Cherry-pick + fork ----------


@router.post("/u/{handle}/subscribe/{feed_id}", response_class=HTMLResponse)
def cherry_pick(
    handle: str,
    feed_id: int,
    request: Request,
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Subscribe the viewer to a feed shown on another user's radar. The
    category is copied from the owner's assignment."""
    owner = resolve_handle(db, handle)
    if not owner or not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if owner.id == viewer.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "that's your own feed")

    row = db.exec(
        select(Subscription, Feed, Category)
        .join(Feed, Feed.id == Subscription.feed_id)
        .join(Category, Category.id == Subscription.category_id)
        .where(Subscription.user_id == owner.id)
        .where(Subscription.feed_id == feed_id)
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not on this radar")
    _sub, feed, category = row

    added = add_subscription_by_feed(db, viewer, feed, category.name)
    if added:
        activity.record(
            db, viewer, "subscribed", feed.title,
            subject_url=feed.site_url or feed.url,
            ref_type="feed", ref_id=feed.id,
        )
    db.commit()
    return templates.TemplateResponse(
        request,
        "_pick_result.html",
        {"added": added, "feed": feed, "category_name": category.name},
    )


@router.post("/u/{handle}/fork", response_class=HTMLResponse)
def fork(
    handle: str,
    request: Request,
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    owner = resolve_handle(db, handle)
    if not owner or not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if owner.id == viewer.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you can't fork yourself")

    result = fork_from(db, viewer, owner)
    if result["added"]:
        activity.record(
            db, viewer, "forked", f"@{owner.handle}",
            subject_url=f"/u/{owner.handle}", ref_type="user", ref_id=owner.id,
        )
        db.commit()
    return templates.TemplateResponse(
        request,
        "_fork_result.html",
        {"result": result, "owner": owner},
    )


# ---------- Activity timeline + RSS ----------


@router.delete("/activity/{event_id}")
def delete_activity(
    event_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Owner-only: erase a specific Activity event. Removes it from
    the owner's /u/{handle}/activity page, /activity.rss, and any
    follower's /feed timeline (all read from the same table)."""
    ev = db.get(Activity, event_id)
    if not ev or ev.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    db.delete(ev)
    db.commit()
    return Response(status_code=200)


def _load_activity(db: Session, owner: User, limit: int = 100) -> list[Activity]:
    return db.exec(
        select(Activity)
        .where(Activity.user_id == owner.id)
        .order_by(Activity.created_at.desc())
        .limit(limit)
    ).all()


@router.get("/u/{handle}/activity", response_class=HTMLResponse)
def user_activity(
    handle: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    owner = resolve_handle(db, handle)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    redirect = maybe_redirect_to_canonical(request, handle, owner)
    if redirect is not None:
        return redirect
    if not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this radar is private")

    # For approved followers viewing a private user, mint the RSS URL that
    # includes their signed token, so they can subscribe from an RSS reader.
    rss_url = f"/u/{owner.handle}/activity.rss"
    if owner.visibility == "private" and viewer and viewer.id != owner.id:
        token = activity.sign_follower(owner, viewer)
        rss_url = f"/u/{owner.handle}/activity.rss?as={viewer.handle}&t={token}"

    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "owner": owner,
            "user": viewer,
            "events": _load_activity(db, owner),
            "kind_icon": activity.KIND_ICON,
            "kind_verb": activity.KIND_VERB,
            "rss_url": rss_url,
        },
    )


@router.get("/u/{handle}/activity.rss", response_class=PlainTextResponse)
def user_activity_rss(
    handle: str,
    request: Request,
    as_: str | None = Query(None, alias="as"),
    t: str | None = None,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    """RSS feed of a user's activity.

    - public/unlisted: open.
    - private: caller must be the owner, an approved follower via session,
      OR present ?as={follower_handle}&t={hmac} tokens that match.
    """
    owner = resolve_handle(db, handle)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    redirect = maybe_redirect_to_canonical(request, handle, owner)
    if redirect is not None:
        return redirect

    allowed = False
    if owner.visibility != "private":  # noqa: SIM114 — keep branches distinct for clarity
        allowed = True
    elif viewer and can_view(viewer, owner, db=db):
        allowed = True
    elif as_ and t:
        follower = db.exec(select(User).where(User.handle == as_)).first()
        if follower and activity.verify_follower_token(owner, follower, t):
            row = db.get(Follow, (follower.id, owner.id))
            if row and row.approved_at is not None:
                allowed = True

    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this activity feed is private")

    events = _load_activity(db, owner, limit=50)
    base = str(request.base_url).rstrip("/")

    def _abs(url: str) -> str:
        if not url:
            return f"{base}/u/{owner.handle}"
        return url if url.startswith(("http://", "https://")) else f"{base}{url}"

    def _rfc822(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    items = []
    for ev in events:
        verb = activity.KIND_VERB.get(ev.kind, ev.kind)
        icon = activity.KIND_ICON.get(ev.kind, "")
        title = f"{icon} @{owner.handle} {verb} {ev.subject}".strip()
        link = _abs(ev.subject_url)
        items.append(
            "    <item>\n"
            f"      <title>{xml_escape(title)}</title>\n"
            f"      <link>{xml_escape(link)}</link>\n"
            f"      <guid isPermaLink=\"false\">activity-{ev.id}</guid>\n"
            f"      <pubDate>{_rfc822(ev.created_at)}</pubDate>\n"
            f"      <description>{xml_escape(title)}</description>\n"
            "    </item>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>@{xml_escape(owner.handle)}'s rssFriends activity</title>\n"
        f"    <link>{base}/u/{xml_escape(owner.handle)}</link>\n"
        f"    <description>Follows, stars, subscriptions and forks by "
        f"@{xml_escape(owner.handle)}</description>\n"
        + "\n".join(items) + "\n"
        "  </channel>\n"
        "</rss>\n"
    )
    return PlainTextResponse(body, media_type="application/rss+xml; charset=utf-8")
