"""Admin-only invites: let a specific email sign up while
RSSFRIENDS_OPEN_REGISTRATION=0. Non-admins get 404 so the page's
existence isn't advertised."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from . import config, mail
from .auth import (
    is_blocked_email,
    is_valid_email,
    issue_magic_link,
    login_required,
    normalize_email,
)
from .collections import _is_admin
from .db import Invite, User, get_session
from .templating import make_templates

templates = make_templates()

router = APIRouter()

RECENT = 30


def _require_admin(db: Session, user: User) -> None:
    if not _is_admin(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND)


def _page(request: Request, db: Session, user: User, **extra) -> HTMLResponse:
    invites = db.exec(select(Invite).order_by(Invite.created_at.desc()).limit(RECENT)).all()
    accounts = {
        u.email: u
        for u in db.exec(select(User).where(User.email.in_([i.email for i in invites]))).all()
    }
    users = db.exec(select(User).order_by(User.created_at.desc()).limit(RECENT)).all()
    response = templates.TemplateResponse(request, "invite.html", {
        "user": user,
        "invites": [(i, accounts.get(i.email)) for i in invites],
        "users": users,
        "open_registration": config.OPEN_REGISTRATION,
        **extra,
    })
    # Lists every member's email — must never sit in a shared cache.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/invite", response_class=HTMLResponse)
def invite_page(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    _require_admin(db, user)
    return _page(request, db, user)


@router.post("/invite", response_class=HTMLResponse)
def send_invite(
    request: Request,
    email: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    _require_admin(db, user)
    email = normalize_email(email)
    if not is_valid_email(email):
        return _page(request, db, user, error="That doesn't look like an email address.")
    if is_blocked_email(email):
        return _page(request, db, user, error="That email domain is blocked.")
    if db.exec(select(User).where(User.email == email)).first():
        return _page(request, db, user, error=f"{email} already has an account.")

    if not db.get(Invite, email):
        db.add(Invite(email=email, invited_by=user.id))
        db.commit()
    try:
        _, url = issue_magic_link(db, email)
    except HTTPException as e:
        return _page(request, db, user, error=str(e.detail))
    mail.send_invite(email, url, user.handle)
    dev_link = url if not config.RESEND_API_KEY and not config.SMTP_HOST else None
    return _page(request, db, user, sent=email, dev_link=dev_link)
