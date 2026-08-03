"""Activity log: recording + signed-token generation for private RSS.

The record() call is fire-and-forget from any write site. Rendering is
done in public.py / templates using the denormalized subject fields —
that way a deleted feed/category still shows up in history sensibly.
"""

import hmac
from hashlib import sha256

from sqlmodel import Session

from .db import Activity, User

# --- Recording -------------------------------------------------------------


def record(
    db: Session,
    user: User,
    kind: str,
    subject: str,
    *,
    subject_url: str = "",
    ref_type: str = "",
    ref_id: int | None = None,
) -> None:
    """Append one activity row. Caller commits."""
    db.add(Activity(
        user_id=user.id,
        kind=kind,
        subject=(subject or "")[:200],
        subject_url=(subject_url or "")[:2000],
        ref_type=ref_type[:20],
        ref_id=ref_id,
    ))


# --- Signed token for private users' activity.rss --------------------------


def sign_follower(owner: User, follower: User) -> str:
    """A short HMAC binding this owner+follower pair. Verifiable without
    a session cookie — that's the whole point, so RSS readers can subscribe."""
    msg = f"{owner.id}:{follower.id}".encode()
    return hmac.new(owner.activity_secret.encode(), msg, sha256).hexdigest()[:16]


def verify_follower_token(owner: User, follower: User, token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(sign_follower(owner, follower), token)


# --- Presentation helpers used by the timeline template -------------------


KIND_ICON = {
    "subscribed": "➕",
    "bookmarked": "⭐",
    "followed": "👤",
    # 0.11.5: user-facing wording moved from "fork" → "copy". Kept the
    # DB kind value "forked" for backward compat with existing rows,
    # RSS descriptions in the wild, and future AP semantics.
    "forked": "📋",
    "created_category": "📁",
    "posted": "🔊",
}


KIND_VERB = {
    "subscribed": "subscribed to",
    "bookmarked": "bookmarked",
    "followed": "followed",
    "forked": "copied the radar of",
    "created_category": "created category",
    "posted": "posted",
}
