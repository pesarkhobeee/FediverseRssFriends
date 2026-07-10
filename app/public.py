"""Public radar page: /u/{handle} + OPML export + visibility settings.

Visibility rules (single dependency, used everywhere a user's radar leaks):
- public   → anyone can view
- unlisted → anyone with the URL can view (not listed on /discover, step 12)
- private  → only the owner (follow-based access lands in step 8)
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .auth import current_user, login_required
from .db import Category, Feed, FeedItem, Subscription, User, get_session
from .importers import add_subscription_by_feed, fork_from
from .utils import relative_time

NEW_BADGE_WINDOW = timedelta(hours=24)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["relative_time"] = relative_time

router = APIRouter()

RECENT_ITEMS_PER_FEED = 5


def can_view(viewer: User | None, target: User) -> bool:
    if target.visibility in ("public", "unlisted"):
        return True
    return bool(viewer and viewer.id == target.id)


def _load_public_radar(db: Session, owner: User, category_filter: str | None) -> dict:
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

    by_cat: dict[int, list[dict]] = {c.id: [] for c in categories}
    for sub, feed in subs_rows:
        entries = items_by_feed.get(feed.id, [])
        by_cat.setdefault(sub.category_id, []).append(
            {"sub": sub, "feed": feed, "entries": entries, "is_new": _is_new(entries)}
        )

    all_categories = db.exec(
        select(Category)
        .where(Category.user_id == owner.id)
        .order_by(Category.sort_order, Category.id)
    ).all()

    total_feeds = db.exec(
        select(Subscription).where(Subscription.user_id == owner.id)
    ).all()

    return {
        "owner": owner,
        "categories_view": [{"category": c, "subs": by_cat.get(c.id, [])} for c in categories],
        "all_categories": all_categories,
        "active_cat": category_filter or "",
        "total_feeds": len(total_feeds),
    }


@router.get("/u/{handle}.opml", response_class=PlainTextResponse)
def user_opml(
    handle: str,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if not can_view(viewer, owner):
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
        f"    <title>@{xml_escape(owner.handle)}'s rssRadar</title>",
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
    if not can_view(viewer, owner):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this radar is private")

    ctx = _load_public_radar(db, owner, cat)
    ctx["user"] = viewer
    ctx["is_owner"] = bool(viewer and viewer.id == owner.id)
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
    if not owner or not can_view(viewer, owner):
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
    if not owner or not can_view(viewer, owner):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if owner.id == viewer.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you can't fork yourself")

    result = fork_from(db, viewer, owner)
    return templates.TemplateResponse(
        request,
        "_fork_result.html",
        {"result": result, "owner": owner},
    )
