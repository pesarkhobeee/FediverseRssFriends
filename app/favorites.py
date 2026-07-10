"""Star/unstar items + the /u/{handle}/favorites page.

Star toggle uses two endpoints (POST to add, DELETE to remove). Each
returns the partial for the *opposite* state so HTMX can swap the
button in place.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from . import activity
from .auth import current_user, login_required
from .db import Favorite, Feed, FeedItem, User, get_session
from .public import can_view
from .utils import relative_time

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters.setdefault("relative_time", relative_time)

router = APIRouter()


def _star(request: Request, item_id: int, is_favorited: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_star.html", {"item_id": item_id, "is_favorited": is_favorited}
    )


@router.post("/items/{item_id}/favorite", response_class=HTMLResponse)
def add_favorite(
    item_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    item = db.get(FeedItem, item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    existing = db.get(Favorite, (user.id, item_id))
    if not existing:
        db.add(Favorite(user_id=user.id, feed_item_id=item_id))
        activity.record(
            db, user, "favorited", item.title,
            subject_url=item.link, ref_type="item", ref_id=item.id,
        )
        db.commit()
    return _star(request, item_id, is_favorited=True)


@router.delete("/items/{item_id}/favorite", response_class=HTMLResponse)
def remove_favorite(
    item_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    existing = db.get(Favorite, (user.id, item_id))
    if existing:
        db.delete(existing)
        db.commit()
    return _star(request, item_id, is_favorited=False)


@router.get("/u/{handle}/favorites", response_class=HTMLResponse)
def user_favorites(
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

    rows = db.exec(
        select(Favorite, FeedItem, Feed)
        .join(FeedItem, FeedItem.id == Favorite.feed_item_id)
        .join(Feed, Feed.id == FeedItem.feed_id)
        .where(Favorite.user_id == owner.id)
        .order_by(Favorite.created_at.desc())
        .limit(200)
    ).all()

    entries = [
        {"fav": fav, "item": item, "feed": feed} for fav, item, feed in rows
    ]

    # For the viewer (if signed in) — which of these items are also in
    # their favorites, so the star renders in the "on" state.
    viewer_faves: set[int] = set()
    if viewer and entries:
        ids = [e["item"].id for e in entries]
        viewer_faves = set(
            db.exec(
                select(Favorite.feed_item_id)
                .where(Favorite.user_id == viewer.id)
                .where(Favorite.feed_item_id.in_(ids))
            ).all()
        )

    return templates.TemplateResponse(
        request,
        "favorites.html",
        {
            "owner": owner,
            "user": viewer,
            "entries": entries,
            "viewer_faves": viewer_faves,
            "is_owner": bool(viewer and viewer.id == owner.id),
        },
    )
