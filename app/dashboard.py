"""Dashboard CRUD: categories + feed subscriptions.

All state-changing routes return small HTML fragments so HTMX can swap
them into the page. Full-page renders live at GET /dashboard.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import update
from sqlmodel import Session, select

from . import activity
from .auth import login_required
from .db import (
    Activity,
    Bookmark,
    Category,
    CategoryFollow,
    Feed,
    FeedItem,
    Follow,
    MagicToken,
    Subscription,
    User,
    get_session,
)
from .feeds import discover_feeds, normalize_feed_url, resolve_avatar, resolve_feed
from .importers import add_subscription_by_url
from .opml import parse_opml
from .refresh import fetch_one, kick_background_refresh, run_cycle
from .templating import make_templates

templates = make_templates()

router = APIRouter()


def _partial(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _load_radar(db: Session, user: User) -> list[dict]:
    """Return categories in order, each with its subscriptions + feed metadata + item count."""
    from sqlalchemy import func

    categories = db.exec(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.sort_order, Category.id)
    ).all()

    subs = db.exec(
        select(Subscription, Feed)
        .join(Feed, Feed.id == Subscription.feed_id)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.sort_order, Subscription.id)
    ).all()

    # Cache item counts per feed in one query.
    feed_ids = {feed.id for _sub, feed in subs}
    counts: dict[int, int] = {}
    if feed_ids:
        rows = db.exec(
            select(FeedItem.feed_id, func.count(FeedItem.id))
            .where(FeedItem.feed_id.in_(feed_ids))
            .group_by(FeedItem.feed_id)
        ).all()
        counts = {fid: n for fid, n in rows}

    by_cat: dict[int, list[dict]] = {c.id: [] for c in categories}
    for sub, feed in subs:
        by_cat.setdefault(sub.category_id, []).append(
            {"sub": sub, "feed": feed, "item_count": counts.get(feed.id, 0)}
        )

    return [{"category": c, "subs": by_cat.get(c.id, [])} for c in categories]


# ---------- Panels ----------
#
# The dashboard is a shell with a tab strip; each tab renders one panel.
# Add a new panel by appending a (key, label, template, loader) row here
# — the tab strip, dispatch, and routing all read from PANELS.

def _load_feeds_context(db: Session, user: User) -> dict:
    return {"radar": _load_radar(db, user)}


def _load_settings_context(db: Session, user: User) -> dict:
    return {}


def _load_import_context(db: Session, user: User) -> dict:
    return {}


def _load_personal_context(db: Session, user: User) -> dict:
    from .personal_feeds import _load_personal_feeds
    return {"personal_feeds": _load_personal_feeds(db, user)}


PANELS: dict[str, dict] = {
    "feeds": {
        "label": "📡 Feeds",
        "template": "_panel_feeds.html",
        "loader": _load_feeds_context,
    },
    "personal": {
        "label": "🎙️ My feeds",
        "template": "_panel_personal.html",
        "loader": _load_personal_context,
    },
    "settings": {
        "label": "⚙️ Settings",
        "template": "_panel_settings.html",
        "loader": _load_settings_context,
    },
    "import": {
        "label": "📥 Import / Export",
        "template": "_panel_import.html",
        "loader": _load_import_context,
    },
}
DEFAULT_PANEL = "feeds"


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    panel: str = DEFAULT_PANEL,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    from .follow import pending_requests_count
    panel_key = panel if panel in PANELS else DEFAULT_PANEL
    panel_ctx = PANELS[panel_key]["loader"](db, user)
    ctx = {
        "user": user,
        "pending_requests": pending_requests_count(db, user.id),
        "panels": PANELS,
        "active_panel": panel_key,
        "panel_template": PANELS[panel_key]["template"],
        **panel_ctx,
    }
    # HTMX tab clicks target #dashboard-body and want the panel only, not
    # the whole shell — check for the hx-request header.
    hx = request.headers.get("HX-Request") == "true"
    hx_target = request.headers.get("HX-Target")
    if hx and hx_target == "dashboard-body":
        return _partial(request, "_dashboard_body.html", **ctx)
    return _partial(request, "dashboard.html", **ctx)


# ---------- Categories ----------


@router.post("/categories", response_class=HTMLResponse)
def create_category(
    request: Request,
    name: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    max_order = db.exec(
        select(Category.sort_order)
        .where(Category.user_id == user.id)
        .order_by(Category.sort_order.desc())
    ).first()
    cat = Category(user_id=user.id, name=name, sort_order=(max_order or 0) + 1)
    db.add(cat)
    db.flush()
    activity.record(db, user, "created_category", cat.name, ref_type="category", ref_id=cat.id)
    db.commit()
    db.refresh(cat)
    return _partial(request, "_category.html", user=user, entry={"category": cat, "subs": []})


@router.delete("/categories/{cat_id}")
def delete_category(
    cat_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    cat = db.get(Category, cat_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    # Cascade the user's subscriptions in this category. Feed rows are global
    # and stay untouched. Also drop any CategoryFollow rows pointed here
    # so we don't leave dangling FKs.
    for sub in db.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.category_id == cat_id)
    ).all():
        db.delete(sub)
    db.exec(
        CategoryFollow.__table__.delete().where(CategoryFollow.category_id == cat_id)
    )
    db.flush()  # ensure child rows are gone before the FK-owning parent
    db.delete(cat)
    db.commit()
    # HTMX will hx-swap the target with the empty body.
    return Response(status_code=200)


@router.get("/categories/{cat_id}/rename-picker", response_class=HTMLResponse)
def category_rename_picker(
    cat_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    cat = db.get(Category, cat_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return _partial(request, "_category_rename_picker.html", category=cat)


@router.get("/categories/{cat_id}/name", response_class=HTMLResponse)
def category_name(
    cat_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Return the default header (name + Rename/Delete buttons). Used by
    Cancel on the rename picker to restore the header without a reload."""
    cat = db.get(Category, cat_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return _partial(request, "_category_header.html", category=cat)


CATEGORY_NAME_MAX = 80


@router.post("/categories/{cat_id}/rename")
def rename_category(
    cat_id: int,
    name: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Rename a category. Enforces the same case-insensitive uniqueness
    per user that find_or_create_category assumes."""
    from sqlalchemy import func

    cat = db.get(Category, cat_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    new_name = (name or "").strip()
    if not new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    if len(new_name) > CATEGORY_NAME_MAX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name too long")
    if new_name.lower() != cat.name.lower():
        dup = db.exec(
            select(Category)
            .where(Category.user_id == user.id)
            .where(func.lower(Category.name) == new_name.lower())
            .where(Category.id != cat_id)
        ).first()
        if dup:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "you already have a category with that name",
            )
    cat.name = new_name
    db.add(cat)
    db.commit()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


# ---------- Subscriptions ----------


def _subscribe_to_url(
    db: Session, user: User, category_id: int, url: str, meta: dict | None = None
) -> tuple[Subscription, Feed]:
    """Shared logic: given a resolved URL, ensure a Feed row exists and
    create the Subscription. Returns (sub, feed). Raises 409 on duplicate."""
    feed = db.exec(select(Feed).where(Feed.url == url)).first()
    if not feed:
        if meta is None:
            meta = resolve_feed(url)
        feed = Feed(
            url=url,
            title=meta["title"],
            site_url=meta["site_url"],
            favicon_url=meta["favicon_url"],
        )
        db.add(feed)
        db.commit()
        db.refresh(feed)

    existing = db.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.feed_id == feed.id)
    ).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "already subscribed")

    max_order = db.exec(
        select(Subscription.sort_order)
        .where(Subscription.user_id == user.id)
        .where(Subscription.category_id == category_id)
        .order_by(Subscription.sort_order.desc())
    ).first()
    sub = Subscription(
        user_id=user.id,
        feed_id=feed.id,
        category_id=category_id,
        sort_order=(max_order or 0) + 1,
    )
    db.add(sub)
    activity.record(
        db, user, "subscribed", feed.title,
        subject_url=feed.site_url or feed.url,
        ref_type="feed", ref_id=feed.id,
    )
    db.commit()
    db.refresh(sub)
    return sub, feed


@router.post("/feeds", response_class=HTMLResponse)
def subscribe(
    request: Request,
    url: str = Form(...),
    category_id: int = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Subscribe by URL. If the pasted URL isn't a feed, run feed
    discovery (parse <link rel="alternate"> + common paths):
      - 0 hits → 400 with a friendly message
      - 1 hit  → silently promote the discovered URL and subscribe
      - N hits → return an inline picker so the user can pick which
    """
    url = normalize_feed_url(url.strip())
    if not url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "url required")

    cat = db.get(Category, category_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")

    # If we've never seen this URL, check whether it's actually a feed.
    # If not, try discovery before creating a broken Feed row.
    if not db.exec(select(Feed).where(Feed.url == url)).first():
        meta = resolve_feed(url)
        # Default to True for backwards compat with older stubs / callers
        # that don't set is_feed; real resolve_feed always sets it.
        if not meta.get("is_feed", True):
            candidates = discover_feeds(url)
            if not candidates:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Couldn't find an RSS feed at that URL. "
                    "Try pasting the feed URL directly.",
                )
            if len(candidates) > 1:
                # Retarget the swap: the form's default target is the
                # feed <ul>; the picker belongs in its own slot next
                # to the form. HTMX honors these response headers.
                resp = _partial(
                    request, "_feed_discovery.html",
                    candidates=candidates,
                    source_url=url,
                    category=cat,
                )
                resp.headers["HX-Retarget"] = f"#discovery-{cat.id}"
                resp.headers["HX-Reswap"] = "innerHTML"
                return resp
            # Exactly one hit — auto-promote and fall through to subscribe.
            url = normalize_feed_url(candidates[0]["url"])
            meta = None  # let _subscribe_to_url re-resolve fresh
        sub, feed = _subscribe_to_url(db, user, category_id, url, meta=meta)
    else:
        sub, feed = _subscribe_to_url(db, user, category_id, url)
    return _partial(request, "_feed_row.html", sub=sub, feed=feed, item_count=0)


@router.post("/feeds/pick", response_class=HTMLResponse)
def subscribe_selected(
    request: Request,
    category_id: int = Form(...),
    urls: list[str] = Form([]),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Subscribe to a set of URLs the user picked from the discovery
    partial. Silently skips duplicates so partial re-submits don't fail."""
    cat = db.get(Category, category_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")

    added_rows: list[tuple[Subscription, Feed]] = []
    seen: set[str] = set()
    for raw in urls:
        u = normalize_feed_url((raw or "").strip())
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            added_rows.append(_subscribe_to_url(db, user, category_id, u))
        except HTTPException as e:
            if e.status_code == status.HTTP_409_CONFLICT:
                continue
            raise

    # Return each added feed row so HTMX appends them to the category list.
    # If nothing was added, return an empty response so the picker just
    # closes (the user un-checked everything before submitting).
    if not added_rows:
        return HTMLResponse("")
    fragments = []
    for sub, feed in added_rows:
        fragments.append(templates.get_template("_feed_row.html").render(
            {"sub": sub, "feed": feed, "item_count": 0, "request": request},
        ))
    return HTMLResponse("\n".join(fragments))


@router.delete("/subscriptions/{sub_id}")
def unsubscribe(
    sub_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    sub = db.get(Subscription, sub_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    db.delete(sub)
    db.commit()
    return Response(status_code=200)


def _get_owned_subscription(db: Session, user: User, sub_id: int) -> Subscription:
    sub = db.get(Subscription, sub_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return sub


@router.get("/subscriptions/{sub_id}/move-picker", response_class=HTMLResponse)
def move_picker(
    sub_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    sub = _get_owned_subscription(db, user, sub_id)
    categories = db.exec(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.sort_order, Category.id)
    ).all()
    return _partial(request, "_move_picker.html", sub=sub, categories=categories)


@router.get("/subscriptions/{sub_id}/actions", response_class=HTMLResponse)
def subscription_actions(
    sub_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Return the default row actions — used by 'Cancel' on the move picker
    to restore the button strip without a full page reload."""
    sub = _get_owned_subscription(db, user, sub_id)
    feed = db.get(Feed, sub.feed_id)
    return _partial(request, "_feed_actions.html", sub=sub, feed=feed)


@router.get("/subscriptions/{sub_id}/edit-url-picker", response_class=HTMLResponse)
def edit_url_picker(
    sub_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    sub = _get_owned_subscription(db, user, sub_id)
    feed = db.get(Feed, sub.feed_id)
    return _partial(request, "_edit_url_picker.html", sub=sub, feed=feed)


@router.post("/subscriptions/{sub_id}/edit-url")
def edit_subscription_url(
    sub_id: int,
    url: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Re-point a subscription at a different feed URL. Never mutates
    the feed for OTHER subscribers.

    Three cases, in order of check:
      1. Normalized new URL matches current → 204 no-op.
      2. A Feed already exists at the new URL → migrate the user's
         subscription to that Feed. If the user already had another
         subscription to it → 409 (duplicate). If the OLD Feed becomes
         orphaned (no remaining subscribers), delete it and its items
         so we don't accumulate zero-subscriber rows.
      3. No Feed at the new URL exists yet →
         a. If the old Feed had NO other subscribers, mutate its URL
            in place (avoids orphan; requested optimization).
         b. If others still use the old Feed, create a new Feed row
            for the new URL and re-point only this user's subscription.
    """
    from sqlalchemy import func

    from .feeds import _fallback_title, normalize_feed_url

    sub = _get_owned_subscription(db, user, sub_id)
    old_feed = db.get(Feed, sub.feed_id)

    new_url = normalize_feed_url((url or "").strip())
    if not new_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "url required")
    if new_url == old_feed.url:
        return Response(status_code=204)  # no-op

    existing_at_new_url = db.exec(
        select(Feed).where(Feed.url == new_url)
    ).first()

    if existing_at_new_url:
        # User can't accidentally end up with two subscriptions to the
        # same feed — schema-level composite index guards this too,
        # but a 409 gives a clean HTMX flow instead of a 500.
        dup = db.exec(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .where(Subscription.feed_id == existing_at_new_url.id)
        ).first()
        if dup and dup.id != sub.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "you already subscribe to that feed",
            )
        sub.feed_id = existing_at_new_url.id
        db.add(sub)
        # If nothing points at the old feed any more, drop it (and its
        # items and any bookmarks from it) to avoid orphan rows.
        _delete_feed_if_orphaned(db, old_feed, exclude_sub_id=sub.id)
    else:
        others = db.exec(
            select(func.count(Subscription.id))
            .where(Subscription.feed_id == old_feed.id)
            .where(Subscription.id != sub.id)
        ).one() or 0
        if others == 0:
            # Requested optimization: mutate the old feed in place.
            # No one else depends on its URL, so this avoids creating
            # a zero-subscriber orphan of the mistyped feed.
            old_feed.url = new_url
            # Blow away metadata that came from the old (wrong) URL so
            # the next refresh cycle re-derives everything fresh.
            old_feed.title = _fallback_title(new_url)
            old_feed.site_url = ""
            old_feed.favicon_url = ""
            old_feed.last_fetched_at = None
            old_feed.last_error = ""
            old_feed.consecutive_failures = 0
            # Purge stale items (they were from the wrong feed) and
            # any Bookmarks that pointed at them. Users who bookmarked
            # items from a mistyped URL lose those — they were
            # already stale, so this is the right thing.
            _purge_feed_items(db, old_feed.id)
        else:
            new_feed = Feed(url=new_url, title=_fallback_title(new_url))
            db.add(new_feed)
            db.flush()
            sub.feed_id = new_feed.id
            db.add(sub)

    db.commit()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


def _purge_feed_items(db: Session, feed_id: int) -> None:
    from .db import Bookmark, FeedItem

    item_ids = [
        row for row in db.exec(
            select(FeedItem.id).where(FeedItem.feed_id == feed_id)
        ).all()
    ]
    if item_ids:
        db.exec(Bookmark.__table__.delete().where(Bookmark.feed_item_id.in_(item_ids)))
        db.exec(FeedItem.__table__.delete().where(FeedItem.feed_id == feed_id))


def _delete_feed_if_orphaned(
    db: Session, feed: Feed, *, exclude_sub_id: int
) -> None:
    """Delete a Feed row and its cascading item + favorite records if
    no subscription (other than `exclude_sub_id`, whose reassignment
    is about to be committed) still points at it."""
    from sqlalchemy import func

    remaining = db.exec(
        select(func.count(Subscription.id))
        .where(Subscription.feed_id == feed.id)
        .where(Subscription.id != exclude_sub_id)
    ).one() or 0
    if remaining > 0:
        return
    _purge_feed_items(db, feed.id)
    db.delete(feed)


@router.post("/subscriptions/{sub_id}/move")
def move_subscription(
    sub_id: int,
    category_id: int = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Move a subscription to a different category on the same radar.
    Both the sub and the target category must belong to the caller."""
    sub = _get_owned_subscription(db, user, sub_id)
    target = db.get(Category, category_id)
    if not target or target.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your category")

    if sub.category_id != target.id:
        # Place the moved feed at the end of the destination's list.
        from sqlalchemy import func
        max_order = db.exec(
            select(func.max(Subscription.sort_order))
            .where(Subscription.user_id == user.id)
            .where(Subscription.category_id == target.id)
        ).one() or 0
        sub.category_id = target.id
        sub.sort_order = max_order + 1
        db.add(sub)
        db.commit()

    # Simplest robust UX for a rare action: tell HTMX to re-fetch the
    # dashboard so both categories re-render with the moved row in its
    # new home. No OOB coordination needed.
    return Response(status_code=204, headers={"HX-Refresh": "true"})


MAX_OPML_BYTES = 2 * 1024 * 1024  # 2 MB is huge for OPML


@router.post("/import/opml", response_class=HTMLResponse)
async def import_opml(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    content = await file.read(MAX_OPML_BYTES + 1)
    if len(content) > MAX_OPML_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "OPML too large")
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    try:
        entries = parse_opml(content)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    added = 0
    dup = 0
    for e in entries:
        was_added, _feed = add_subscription_by_url(
            db, user, e.xml_url, e.category,
            title_hint=e.title or None,
            site_hint=e.html_url or None,
        )
        if was_added:
            added += 1
        else:
            dup += 1
    db.commit()

    if added:
        kick_background_refresh()

    return _partial(
        request,
        "_import_result.html",
        result={"added": added, "duplicates": dup, "total": len(entries)},
    )


@router.post("/feeds/{feed_id}/refresh", response_class=HTMLResponse)
def refresh_single_feed(
    feed_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Force a synchronous refresh of a single feed. The caller must
    either be subscribed to the feed OR have it registered as one of
    their personal feeds — feeds are global, so any authorized user
    can trigger a refetch."""
    from .db import PersonalFeed

    authorized = db.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.feed_id == feed_id)
    ).first() or db.exec(
        select(PersonalFeed)
        .where(PersonalFeed.user_id == user.id)
        .where(PersonalFeed.feed_id == feed_id)
    ).first()
    if not authorized:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not authorized")
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not found")

    new, err = fetch_one(db, feed)
    # Manual refresh is a good moment to upgrade the avatar too — the
    # scheduled cycle only does the fast path, so YouTube channels stay
    # generic without this.
    deep_avatar = resolve_avatar(feed.url, feed.site_url, None, deep=True)
    if deep_avatar and deep_avatar != feed.favicon_url:
        feed.favicon_url = deep_avatar
        db.add(feed)
        db.commit()
    return _partial(
        request,
        "_feed_refreshed.html",
        new=new,
        err=err,
        feed=feed,
    )


# ---------- Account deletion ----------


def _account_footprint(db: Session, user: User) -> dict[str, int]:
    """Counts we show the user on the confirmation screen so they know
    exactly what's about to be wiped. Cheap aggregate queries — one
    row per counter."""
    def n(stmt) -> int:
        return db.exec(stmt).one() or 0

    from sqlalchemy import func
    return {
        "subscriptions": n(select(func.count(Subscription.id))
                           .where(Subscription.user_id == user.id)),
        "categories": n(select(func.count(Category.id))
                        .where(Category.user_id == user.id)),
        "bookmarks": n(select(func.count()).select_from(Bookmark)
                       .where(Bookmark.user_id == user.id)),
        "following": n(select(func.count()).select_from(Follow)
                       .where(Follow.follower_id == user.id)),
        "followers": n(select(func.count()).select_from(Follow)
                       .where(Follow.followee_id == user.id)),
        "activity": n(select(func.count(Activity.id))
                      .where(Activity.user_id == user.id)),
    }


def _delete_user_cascade(db: Session, user: User) -> None:
    """Wipe every row this user owns, and null out foreign references
    from third-party rows. Feeds/FeedItems are shared and untouched —
    a feed with zero subscribers just stops being refreshed elsewhere.

    Third-party Activity rows that named this user as `ref_id` (e.g.
    "@bob forked @alice") keep their denormalized subject text and get
    `ref_id` set to NULL — so timelines don't dangle-point at a
    nonexistent row but history remains readable."""
    uid = user.id
    email = user.email
    # Category-follows first: they FK to Category (which we drop below)
    # and to User (as follower). Cover both sides:
    #   - rows where the deleted user is the follower
    #   - rows on categories owned by the deleted user
    for stmt in (
        CategoryFollow.__table__.delete().where(CategoryFollow.follower_id == uid),
        CategoryFollow.__table__.delete().where(
            CategoryFollow.category_id.in_(
                select(Category.id).where(Category.user_id == uid)
            )
        ),
        Bookmark.__table__.delete().where(Bookmark.user_id == uid),
        Subscription.__table__.delete().where(Subscription.user_id == uid),
        Category.__table__.delete().where(Category.user_id == uid),
        Follow.__table__.delete().where(
            (Follow.follower_id == uid) | (Follow.followee_id == uid)
        ),
        Activity.__table__.delete().where(Activity.user_id == uid),
        update(Activity).where(
            Activity.ref_type == "user", Activity.ref_id == uid
        ).values(ref_id=None),
        MagicToken.__table__.delete().where(MagicToken.email == email),
        User.__table__.delete().where(User.id == uid),
    ):
        db.exec(stmt)
    db.commit()


@router.get("/account/delete-confirm", response_class=HTMLResponse)
def delete_confirm(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    return _partial(
        request,
        "_delete_account.html",
        user=user,
        footprint=_account_footprint(db, user),
    )


@router.post("/account/delete")
def delete_account(
    request: Request,
    confirm_handle: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Destructive. Requires the user to re-type their handle as a
    second affirmation — the client-side JS already enforces this,
    but we re-check server-side so a bypassed form can't sneak
    through."""
    if confirm_handle.strip().lstrip("@").lower() != user.handle.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "handle does not match — account not deleted",
        )
    _delete_user_cascade(db, user)
    request.session.clear()
    return RedirectResponse("/?deleted=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/refresh", response_class=HTMLResponse)
def manual_refresh(
    request: Request,
    _user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Kick off one refresh cycle synchronously. Returns the fresh dashboard
    body so HTMX can swap it in place."""
    result = run_cycle()
    return _partial(
        request,
        "_refresh_flash.html",
        result=result,
    )
