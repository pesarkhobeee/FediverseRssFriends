"""Public radar page: /u/{handle} + OPML export + visibility settings.

Visibility rules (single dependency, used everywhere a user's radar leaks):
- public   → anyone can view
- unlisted → anyone with the URL can view (not listed on /discover, step 12)
- private  → only the owner (follow-based access lands in step 8)
"""

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
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")

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
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
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
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
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


@router.get("/u/{handle}", response_class=HTMLResponse)
def user_radar(
    handle: str,
    request: Request,
    cat: str | None = None,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")

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

    ctx = _load_public_radar(db, owner, cat, viewer=viewer)
    ctx["user"] = viewer
    ctx["is_owner"] = bool(viewer and viewer.id == owner.id)
    ctx["follow_state"] = follow_state(db, viewer.id, owner.id) if viewer else "none"
    ctx["followers_n"] = follower_count(db, owner.id)
    ctx["following_n"] = following_count(db, owner.id)
    ctx["reading_rss_url"] = _signed_url(f"/u/{owner.handle}.rss")
    ctx["activity_rss_url"] = _signed_url(f"/u/{owner.handle}/activity.rss")
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
    owner = db.exec(select(User).where(User.handle == handle)).first()
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
    owner = db.exec(select(User).where(User.handle == handle)).first()
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
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
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
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")

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
