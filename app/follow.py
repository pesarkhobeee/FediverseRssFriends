"""Follow graph: follow/unfollow, pending requests for private users,
approve/reject inbox, followers + following lists.

Approval rules (matches visibility semantics):
  - target public   → auto-approve on follow.
  - target unlisted → auto-approve on follow.
  - target private  → row inserted with approved_at=NULL until owner acts.

Approved followers of a private user gain view access via can_view().
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session, func, select

from . import activity, mail
from .auth import current_user, login_required
from .db import Category, CategoryFollow, Follow, User, _now, get_session
from .templating import make_templates

templates = make_templates()

router = APIRouter()


def is_approved_follower(db: Session, viewer_id: int, target_id: int) -> bool:
    row = db.get(Follow, (viewer_id, target_id))
    return bool(row and row.approved_at is not None)


def follow_state(db: Session, viewer_id: int, target_id: int) -> str:
    """Return 'self' | 'following' | 'pending' | 'none'."""
    if viewer_id == target_id:
        return "self"
    row = db.get(Follow, (viewer_id, target_id))
    if not row:
        return "none"
    return "following" if row.approved_at else "pending"


def follower_count(db: Session, user_id: int) -> int:
    return db.exec(
        select(func.count(Follow.follower_id))
        .where(Follow.followee_id == user_id)
        .where(Follow.approved_at.is_not(None))
    ).one()


def following_count(db: Session, user_id: int) -> int:
    return db.exec(
        select(func.count(Follow.followee_id))
        .where(Follow.follower_id == user_id)
        .where(Follow.approved_at.is_not(None))
    ).one()


def pending_requests_count(db: Session, user_id: int) -> int:
    return db.exec(
        select(func.count(Follow.follower_id))
        .where(Follow.followee_id == user_id)
        .where(Follow.approved_at.is_(None))
    ).one()


def _button_partial(request: Request, target: User, state: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_follow_button.html", {"target": target, "state": state}
    )


@router.post("/u/{handle}/follow", response_class=HTMLResponse)
def follow(
    handle: str,
    request: Request,
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    target = db.exec(select(User).where(User.handle == handle)).first()
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if target.id == viewer.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "can't follow yourself")

    existing = db.get(Follow, (viewer.id, target.id))
    is_new = existing is None
    if is_new:
        approved = _now() if target.visibility != "private" else None
        db.add(Follow(follower_id=viewer.id, followee_id=target.id, approved_at=approved))
        if approved:
            activity.record(
                db, viewer, "followed", f"@{target.handle}",
                subject_url=f"/u/{target.handle}", ref_type="user", ref_id=target.id,
            )
        db.commit()
        state = "following" if approved else "pending"
        # Notify the followee — auto-approved followers or new pending
        # requests. Failure is logged, never propagated.
        mail.send_new_follower(
            followee_email=target.email,
            followee_handle=target.handle,
            follower_handle=viewer.handle,
            approved=bool(approved),
        )
    else:
        state = "following" if existing.approved_at else "pending"

    return _button_partial(request, target, state)


@router.delete("/u/{handle}/follow", response_class=HTMLResponse)
def unfollow(
    handle: str,
    request: Request,
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    target = db.exec(select(User).where(User.handle == handle)).first()
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    row = db.get(Follow, (viewer.id, target.id))
    if row:
        db.delete(row)
        db.commit()
    return _button_partial(request, target, "none")


# ---------- Category follows ----------


def category_follow_state(db: Session, viewer_id: int, category_id: int) -> str:
    """'following' if this viewer follows this specific category,
    else 'none'. Doesn't inspect whole-user follow state — the two
    graphs are independent (following the whole person is a strict
    superset of following one of their categories)."""
    row = db.exec(
        select(CategoryFollow)
        .where(CategoryFollow.follower_id == viewer_id)
        .where(CategoryFollow.category_id == category_id)
    ).first()
    return "following" if row else "none"


def _category_follow_partial(
    request: Request, category: Category, owner_handle: str, state: str
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_category_follow_button.html",
        {"category": category, "owner_handle": owner_handle, "state": state},
    )


@router.post("/u/{handle}/c/{cat_id}/follow", response_class=HTMLResponse)
def follow_category(
    handle: str,
    cat_id: int,
    request: Request,
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Follow one specific category on someone else's radar. Requires
    the follower to have view access (same can_view rule) — public and
    unlisted work directly; private users need an approved whole-user
    follow first."""
    from .public import can_view

    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if owner.id == viewer.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "can't follow your own category")
    if not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to this radar")

    cat = db.get(Category, cat_id)
    if not cat or cat.user_id != owner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")

    existing = db.exec(
        select(CategoryFollow)
        .where(CategoryFollow.follower_id == viewer.id)
        .where(CategoryFollow.category_id == cat_id)
    ).first()
    if not existing:
        db.add(CategoryFollow(follower_id=viewer.id, category_id=cat_id))
        db.commit()
    return _category_follow_partial(request, cat, owner.handle, "following")


@router.delete("/u/{handle}/c/{cat_id}/follow", response_class=HTMLResponse)
def unfollow_category(
    handle: str,
    cat_id: int,
    request: Request,
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    cat = db.get(Category, cat_id)
    if not cat or cat.user_id != owner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")
    row = db.exec(
        select(CategoryFollow)
        .where(CategoryFollow.follower_id == viewer.id)
        .where(CategoryFollow.category_id == cat_id)
    ).first()
    if row:
        db.delete(row)
        db.commit()
    return _category_follow_partial(request, cat, owner.handle, "none")


@router.get("/follow-requests", response_class=HTMLResponse)
def requests_inbox(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    rows = db.exec(
        select(Follow, User)
        .join(User, User.id == Follow.follower_id)
        .where(Follow.followee_id == user.id)
        .where(Follow.approved_at.is_(None))
        .order_by(Follow.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "follow_requests.html",
        {"user": user, "requests": [{"follow": f, "follower": u} for f, u in rows]},
    )


@router.post("/follow-requests/{follower_id}/approve", response_class=HTMLResponse)
def approve_request(
    follower_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    row = db.get(Follow, (follower_id, user.id))
    if not row or row.approved_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no pending request")
    row.approved_at = _now()
    db.add(row)
    # Log on the *follower's* activity — "@X followed @user".
    follower = db.get(User, follower_id)
    if follower:
        activity.record(
            db, follower, "followed", f"@{user.handle}",
            subject_url=f"/u/{user.handle}", ref_type="user", ref_id=user.id,
        )
    db.commit()
    if follower:
        mail.send_follow_approved(
            follower_email=follower.email,
            follower_handle=follower.handle,
            followee_handle=user.handle,
        )
    return HTMLResponse("")  # HTMX will delete the row


@router.post("/follow-requests/{follower_id}/reject", response_class=HTMLResponse)
def reject_request(
    follower_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    row = db.get(Follow, (follower_id, user.id))
    if not row or row.approved_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no pending request")
    db.delete(row)
    db.commit()
    return HTMLResponse("")


@router.post("/followers/{follower_id}/revoke", response_class=HTMLResponse)
def revoke_follower(
    follower_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Owner removes an already-approved follower. Same effect as if the
    follower had unfollowed themselves — the row is deleted."""
    row = db.get(Follow, (follower_id, user.id))
    if not row or row.approved_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not an approved follower")
    db.delete(row)
    db.commit()
    return HTMLResponse("")


@router.get("/u/{handle}/followers", response_class=HTMLResponse)
def followers_page(
    handle: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    from .public import can_view  # avoid cycle at import time

    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this radar is private")
    rows = db.exec(
        select(Follow, User)
        .join(User, User.id == Follow.follower_id)
        .where(Follow.followee_id == owner.id)
        .where(Follow.approved_at.is_not(None))
        .order_by(Follow.approved_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "follow_list.html",
        {
            "user": viewer,
            "owner": owner,
            "kind": "followers",
            "entries": [{"follow": f, "other": u} for f, u in rows],
            "is_owner": bool(viewer and viewer.id == owner.id),
        },
    )


@router.get("/u/{handle}/following", response_class=HTMLResponse)
def following_page(
    handle: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    from .public import can_view

    owner = db.exec(select(User).where(User.handle == handle)).first()
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if not can_view(viewer, owner, db=db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this radar is private")
    rows = db.exec(
        select(Follow, User)
        .join(User, User.id == Follow.followee_id)
        .where(Follow.follower_id == owner.id)
        .where(Follow.approved_at.is_not(None))
        .order_by(Follow.approved_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "follow_list.html",
        {
            "user": viewer,
            "owner": owner,
            "kind": "following",
            "entries": [{"follow": f, "other": u} for f, u in rows],
            "is_owner": bool(viewer and viewer.id == owner.id),
        },
    )
