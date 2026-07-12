"""/discover — cold-start discovery. Public users, popular feeds, most-forked
curators. Anonymous-visitable so search engines and unregistered users can
find their way in."""

from difflib import SequenceMatcher
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, or_, select

from .auth import current_user
from .db import Activity, Feed, Follow, Subscription, User, get_session
from .utils import relative_time

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters.setdefault("relative_time", relative_time)

router = APIRouter()

LIMIT = 20


def _recent_public_users(db: Session) -> list[dict]:
    users = db.exec(
        select(User)
        .where(User.visibility == "public")
        .order_by(User.created_at.desc())
        .limit(LIMIT)
    ).all()
    if not users:
        return []
    ids = [u.id for u in users]
    # One grouped query for follower counts.
    fc_rows = db.exec(
        select(Follow.followee_id, func.count(Follow.follower_id))
        .where(Follow.followee_id.in_(ids))
        .where(Follow.approved_at.is_not(None))
        .group_by(Follow.followee_id)
    ).all()
    followers_by_id = {uid: n for uid, n in fc_rows}
    sub_rows = db.exec(
        select(Subscription.user_id, func.count(Subscription.id))
        .where(Subscription.user_id.in_(ids))
        .group_by(Subscription.user_id)
    ).all()
    subs_by_id = {uid: n for uid, n in sub_rows}
    return [
        {
            "user": u,
            "followers": followers_by_id.get(u.id, 0),
            "feeds": subs_by_id.get(u.id, 0),
        }
        for u in users
    ]


def _popular_feeds(db: Session) -> list[dict]:
    # Count subscriptions only from public users so private radars don't
    # leak popularity signals via discover.
    rows = db.exec(
        select(Feed, func.count(Subscription.id).label("n"))
        .join(Subscription, Subscription.feed_id == Feed.id)
        .join(User, User.id == Subscription.user_id)
        .where(User.visibility == "public")
        .group_by(Feed.id)
        .order_by(func.count(Subscription.id).desc(), Feed.id.desc())
        .limit(LIMIT)
    ).all()
    return [{"feed": feed, "subscribers": n} for feed, n in rows]


def _most_forked_curators(db: Session) -> list[dict]:
    rows = db.exec(
        select(User, func.count(Activity.id).label("n"))
        .join(Activity, Activity.ref_id == User.id)
        .where(Activity.kind == "forked")
        .where(Activity.ref_type == "user")
        .where(User.visibility == "public")
        .group_by(User.id)
        .order_by(func.count(Activity.id).desc(), User.id.desc())
        .limit(10)
    ).all()
    return [{"user": user, "forks": n} for user, n in rows]


SEARCH_MAX_RESULTS = 15
SEARCH_MIN_SIMILARITY = 0.35


def _search_public_users(db: Session, q: str) -> list[User]:
    """Fuzzy search by handle. Substring LIKE first (fast + covers the
    common case), then re-rank by difflib similarity. Private + unlisted
    users are excluded so search doesn't leak them."""
    q = (q or "").strip().lstrip("@").lower()
    if not q:
        return []

    pattern = f"%{q}%"
    hits = db.exec(
        select(User)
        .where(User.visibility == "public")
        .where(
            or_(
                func.lower(User.handle).like(pattern),
                func.lower(User.email).like(pattern),
            )
        )
        .limit(50)
    ).all()

    # If nothing substring-matched, do a wider difflib scan across ALL
    # public users. Bounded at instance size — fine at MVP scale; when
    # we outgrow it we can swap to SQLite FTS5.
    if not hits:
        hits = db.exec(
            select(User)
            .where(User.visibility == "public")
            .limit(2000)
        ).all()

    def _similarity(u: User) -> float:
        return SequenceMatcher(None, q, u.handle.lower()).ratio()

    ranked = sorted(hits, key=_similarity, reverse=True)
    ranked = [u for u in ranked if _similarity(u) >= SEARCH_MIN_SIMILARITY]
    return ranked[:SEARCH_MAX_RESULTS]


@router.get("/discover", response_class=HTMLResponse)
def discover(
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "discover.html",
        {
            "user": viewer,
            "recent_users": _recent_public_users(db),
            "popular_feeds": _popular_feeds(db),
            "most_forked": _most_forked_curators(db),
        },
    )


@router.get("/discover/search", response_class=HTMLResponse)
def discover_search(
    request: Request,
    q: str = Query("", max_length=100),
    _viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    """HTMX endpoint: returns a partial list of users matching `q`."""
    return templates.TemplateResponse(
        request,
        "_discover_search_results.html",
        {"q": q.strip(), "results": _search_public_users(db, q)},
    )
