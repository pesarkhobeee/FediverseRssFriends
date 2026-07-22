"""Bookmark/unbookmark items + the /u/{handle}/bookmarks page.

Toggle uses two endpoints (POST to add, DELETE to remove). Each
returns the partial for the *opposite* state so HTMX can swap the
button in place.

Renamed from "favorites" in 0.2.36 to soften the semantics — a
bookmark is "I saved this to find later" rather than "I loved this."
Old URLs (/items/{id}/favorite, /u/{handle}/favorites) still work via
301 redirects.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from . import activity, wallabag
from .auth import current_user, login_required
from .db import Bookmark, Category, Feed, FeedItem, Subscription, User, get_session
from .public import can_view
from .templating import make_templates

templates = make_templates()

router = APIRouter()


def _bookmark_partial(request: Request, item_id: int, is_bookmarked: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_bookmark.html",
        {"item_id": item_id, "is_bookmarked": is_bookmarked},
    )


@router.post("/items/{item_id}/bookmark", response_class=HTMLResponse)
def add_bookmark(
    item_id: int,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    item = db.get(FeedItem, item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    existing = db.get(Bookmark, (user.id, item_id))
    if not existing:
        db.add(Bookmark(user_id=user.id, feed_item_id=item_id))
        activity.record(
            db, user, "bookmarked", item.title,
            subject_url=item.link, ref_type="item", ref_id=item.id,
        )
        db.commit()
        # Best-effort: send to Wallabag if user has it enabled. We look
        # up the user's own category name for this feed (if any) so it
        # can be sent as a tag. No-ops for users without Wallabag.
        if item.link:
            tag_name = _viewer_category_for_feed(db, user.id, item.feed_id)
            background.add_task(
                wallabag.send_url_safe,
                user.id, item.link,
                [tag_name] if tag_name else None,
            )
    return _bookmark_partial(request, item_id, is_bookmarked=True)


def _viewer_category_for_feed(db: Session, user_id: int, feed_id: int) -> str | None:
    row = db.exec(
        select(Category.name)
        .join(Subscription, Subscription.category_id == Category.id)
        .where(Subscription.user_id == user_id)
        .where(Subscription.feed_id == feed_id)
    ).first()
    return row if row else None


@router.delete("/items/{item_id}/bookmark", response_class=HTMLResponse)
def remove_bookmark(
    item_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    existing = db.get(Bookmark, (user.id, item_id))
    if existing:
        db.delete(existing)
        db.commit()
    return _bookmark_partial(request, item_id, is_bookmarked=False)


@router.get("/u/{handle}/bookmarks", response_class=HTMLResponse)
def user_bookmarks(
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
        select(Bookmark, FeedItem, Feed)
        .join(FeedItem, FeedItem.id == Bookmark.feed_item_id)
        .join(Feed, Feed.id == FeedItem.feed_id)
        .where(Bookmark.user_id == owner.id)
        .order_by(Bookmark.created_at.desc())
        .limit(200)
    ).all()

    entries = [
        {"bookmark": bm, "item": item, "feed": feed} for bm, item, feed in rows
    ]

    # For the viewer (if signed in) — which of these items are also in
    # their bookmarks, so the icon renders in the "on" state.
    viewer_bookmarks: set[int] = set()
    if viewer and entries:
        ids = [e["item"].id for e in entries]
        viewer_bookmarks = set(
            db.exec(
                select(Bookmark.feed_item_id)
                .where(Bookmark.user_id == viewer.id)
                .where(Bookmark.feed_item_id.in_(ids))
            ).all()
        )

    return templates.TemplateResponse(
        request,
        "bookmarks.html",
        {
            "owner": owner,
            "user": viewer,
            "entries": entries,
            "viewer_bookmarks": viewer_bookmarks,
            "is_owner": bool(viewer and viewer.id == owner.id),
        },
    )


# ---- Backwards-compat redirects for the old /favorites URLs ----------

@router.post("/items/{item_id}/favorite", include_in_schema=False)
def compat_add_favorite(item_id: int):
    return RedirectResponse(
        f"/items/{item_id}/bookmark", status_code=status.HTTP_308_PERMANENT_REDIRECT,
    )


@router.delete("/items/{item_id}/favorite", include_in_schema=False)
def compat_remove_favorite(item_id: int):
    return RedirectResponse(
        f"/items/{item_id}/bookmark", status_code=status.HTTP_308_PERMANENT_REDIRECT,
    )


@router.get("/u/{handle}/favorites", include_in_schema=False)
def compat_user_favorites(handle: str):
    return RedirectResponse(
        f"/u/{handle}/bookmarks", status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )
