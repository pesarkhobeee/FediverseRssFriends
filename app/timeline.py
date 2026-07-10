"""Home timeline at /feed — signed-in users' merged view of:
  - activity from users they follow (approved), and
  - fresh items from the feeds they subscribe to.

Both streams are pulled from the same recent window, interleaved by
timestamp, newest first. This is the app's default page for signed-in
users; anonymous visitors keep landing on the marketing/signup page.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from . import activity
from .auth import login_required
from .db import Activity, Favorite, Feed, FeedItem, Follow, Subscription, User, get_session
from .utils import relative_time

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters.setdefault("relative_time", relative_time)

router = APIRouter()

FEED_WINDOW_DAYS = 30
FEED_LIMIT = 100


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _load_timeline(db: Session, user: User) -> dict:
    cutoff = datetime.now(UTC) - timedelta(days=FEED_WINDOW_DAYS)

    # Who am I following (approved only)?
    followee_ids = list(
        db.exec(
            select(Follow.followee_id)
            .where(Follow.follower_id == user.id)
            .where(Follow.approved_at.is_not(None))
        ).all()
    )
    handle_by_id = {}
    if followee_ids:
        users = db.exec(select(User).where(User.id.in_(followee_ids))).all()
        handle_by_id = {u.id: u for u in users}

    activity_entries: list[dict] = []
    if followee_ids:
        rows = db.exec(
            select(Activity)
            .where(Activity.user_id.in_(followee_ids))
            .where(Activity.created_at >= cutoff)
            .order_by(Activity.created_at.desc())
            .limit(FEED_LIMIT)
        ).all()
        for ev in rows:
            actor = handle_by_id.get(ev.user_id)
            activity_entries.append({
                "kind": "activity",
                "ts": _aware(ev.created_at),
                "actor": actor,
                "activity": ev,
            })

    # Fresh items from my own subscriptions.
    my_feed_ids = list(
        db.exec(
            select(Subscription.feed_id).where(Subscription.user_id == user.id)
        ).all()
    )
    item_entries: list[dict] = []
    feed_by_id: dict[int, Feed] = {}
    if my_feed_ids:
        feed_by_id = {
            f.id: f for f in db.exec(select(Feed).where(Feed.id.in_(my_feed_ids))).all()
        }
        items = db.exec(
            select(FeedItem)
            .where(FeedItem.feed_id.in_(my_feed_ids))
            .where(
                (FeedItem.published_at.is_(None))
                | (FeedItem.published_at >= cutoff)
            )
            .order_by(
                FeedItem.published_at.desc().nulls_last(),
                FeedItem.fetched_at.desc(),
            )
            .limit(FEED_LIMIT)
        ).all()
        # Which of these has the viewer already starred?
        item_ids = [i.id for i in items]
        faves: set[int] = set()
        if item_ids:
            faves = set(
                db.exec(
                    select(Favorite.feed_item_id)
                    .where(Favorite.user_id == user.id)
                    .where(Favorite.feed_item_id.in_(item_ids))
                ).all()
            )
        for it in items:
            ts = _aware(it.published_at or it.fetched_at)
            item_entries.append({
                "kind": "item",
                "ts": ts,
                "item": it,
                "feed": feed_by_id.get(it.feed_id),
                "is_favorited": it.id in faves,
            })

    merged = sorted(
        activity_entries + item_entries, key=lambda e: e["ts"], reverse=True
    )[:FEED_LIMIT]

    return {
        "entries": merged,
        "n_followees": len(followee_ids),
        "n_subscriptions": len(my_feed_ids),
        "kind_icon": activity.KIND_ICON,
        "kind_verb": activity.KIND_VERB,
    }


@router.get("/feed", response_class=HTMLResponse)
def home_timeline(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    ctx = _load_timeline(db, user)
    ctx["user"] = user
    return templates.TemplateResponse(request, "timeline.html", ctx)
