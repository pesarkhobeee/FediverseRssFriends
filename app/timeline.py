"""Home timeline at /feed — signed-in users' view of what the people
they follow have been doing: starring items, subscribing to feeds,
following others, forking radars.

Previously this also merged fresh items from the viewer's own
subscribed feeds, but that firehose drowned the social signal —
subscribed-item browsing lives on /u/{handle} and /dashboard.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from . import activity
from .auth import login_required
from .db import Activity, Follow, User, get_session
from .templating import make_templates

templates = make_templates()

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
    handle_by_id: dict[int, User] = {}
    if followee_ids:
        users = db.exec(select(User).where(User.id.in_(followee_ids))).all()
        handle_by_id = {u.id: u for u in users}

    entries: list[dict] = []
    if followee_ids:
        rows = db.exec(
            select(Activity)
            .where(Activity.user_id.in_(followee_ids))
            .where(Activity.created_at >= cutoff)
            .order_by(Activity.created_at.desc())
            .limit(FEED_LIMIT)
        ).all()
        for ev in rows:
            entries.append({
                "kind": "activity",
                "ts": _aware(ev.created_at),
                "actor": handle_by_id.get(ev.user_id),
                "activity": ev,
            })

    return {
        "entries": entries,
        "n_followees": len(followee_ids),
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
