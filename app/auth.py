"""Magic-link auth: token gen, verify, session, current_user dependency."""

import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from . import config
from .db import MagicToken, User, _now, get_session

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _aware(dt: datetime) -> datetime:
    """SQLite strips tzinfo on read — coerce naive values back to UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email)) and len(email) <= 254


def _slugify_handle(email: str) -> str:
    base = email.split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base or "user"


def _unique_handle(db: Session, email: str) -> str:
    base = _slugify_handle(email)
    handle = base
    i = 2
    while db.exec(select(User).where(User.handle == handle)).first():
        handle = f"{base}-{i}"
        i += 1
    return handle


def issue_magic_link(db: Session, email: str) -> tuple[str, str]:
    """Return (token, verify_url). Rate-limited per-email."""
    email = normalize_email(email)
    if not is_valid_email(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid email")

    # Rate limit: reject if a fresh token was minted very recently.
    recent = db.exec(
        select(MagicToken)
        .where(MagicToken.email == email)
        .order_by(MagicToken.created_at.desc())
    ).first()
    if recent:
        age = (_now() - _aware(recent.created_at)).total_seconds()
        if age < config.MAGIC_LINK_MIN_INTERVAL_SECONDS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "please wait a moment")

    token = secrets.token_urlsafe(32)
    row = MagicToken(
        token=token,
        email=email,
        expires_at=_now() + timedelta(minutes=config.MAGIC_LINK_TTL_MINUTES),
    )
    db.add(row)
    db.commit()

    url = f"{config.BASE_URL.rstrip('/')}/auth/verify?token={token}"
    return token, url


def consume_token(db: Session, token: str) -> User:
    """Validate and single-use-consume a magic token. Returns the User (creating if new)."""
    row = db.get(MagicToken, token)
    if not row:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired link")

    if _aware(row.expires_at) < _now():
        db.delete(row)
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired link")

    email = row.email
    db.delete(row)  # single-use

    user = db.exec(select(User).where(User.email == email)).first()
    if not user:
        user = User(email=email, handle=_unique_handle(db, email))
        db.add(user)

    db.commit()
    if user.id is None:
        db.refresh(user)
    return user


def current_user(
    request: Request, db: Session = Depends(get_session)
) -> User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    return db.get(User, uid)


def login_required(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    return user
