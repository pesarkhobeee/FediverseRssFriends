"""Wallabag OAuth2 client + bookmark dispatcher.

Users link their Wallabag account via authorization-code flow: they
paste their instance URL + a client_id/client_secret they minted on
their instance's /developer page, we redirect them to the instance
for consent, then exchange the returned code for a token pair.

When a user bookmarks an item and has an enabled WallabagAccount, we
enqueue a background task that POSTs the item's URL to their
/api/entries.json. Errors are logged and swallowed — the bookmark
row itself is the source of truth; Wallabag delivery is best-effort.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, select

from .config import SESSION_SECRET
from .db import User, WallabagAccount, engine

log = logging.getLogger(__name__)

# Give ourselves a small buffer before token expiry so refreshes happen
# proactively instead of racing the first 401.
_REFRESH_LEEWAY_SECONDS = 60
_HTTP_TIMEOUT = 10.0

# Signed state param for the OAuth redirect. Tied to user id so the
# callback can't be replayed under a different session.
_STATE_SALT = "wallabag-oauth-state"
_STATE_MAX_AGE = 15 * 60  # 15 minutes


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(SESSION_SECRET, salt=_STATE_SALT)


def sign_state(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def verify_state(token: str) -> int | None:
    try:
        data = _serializer().loads(token, max_age=_STATE_MAX_AGE)
    except BadSignature:
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None


def _normalize_instance(url: str) -> str:
    return url.strip().rstrip("/")


def build_authorize_url(account: WallabagAccount, redirect_uri: str) -> str:
    """URL to redirect the user to on their Wallabag instance for consent."""
    from urllib.parse import urlencode

    params = {
        "client_id": account.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": sign_state(account.user_id),
    }
    return f"{_normalize_instance(account.instance_url)}/oauth/v2/auth?{urlencode(params)}"


def _store_tokens(db: Session, account: WallabagAccount, payload: dict) -> None:
    access = payload.get("access_token") or ""
    refresh = payload.get("refresh_token") or ""
    expires_in = int(payload.get("expires_in") or 0)
    account.access_token = access
    account.refresh_token = refresh or account.refresh_token
    account.token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    account.enabled = True
    account.updated_at = datetime.now(UTC)
    db.add(account)


def exchange_code(
    db: Session, account: WallabagAccount, code: str, redirect_uri: str,
) -> bool:
    """POST /oauth/v2/token with grant_type=authorization_code.

    Returns True on success. On failure logs and returns False without
    mutating the account row."""
    url = f"{_normalize_instance(account.instance_url)}/oauth/v2/token"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.post(url, data={
                "grant_type": "authorization_code",
                "client_id": account.client_id,
                "client_secret": account.client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            })
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("wallabag: code exchange failed for user %s: %s",
                    account.user_id, exc)
        return False
    _store_tokens(db, account, payload)
    db.commit()
    return True


def _refresh(db: Session, account: WallabagAccount) -> bool:
    if not account.refresh_token:
        return False
    url = f"{_normalize_instance(account.instance_url)}/oauth/v2/token"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.post(url, data={
                "grant_type": "refresh_token",
                "client_id": account.client_id,
                "client_secret": account.client_secret,
                "refresh_token": account.refresh_token,
            })
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("wallabag: refresh failed for user %s: %s",
                    account.user_id, exc)
        return False
    _store_tokens(db, account, payload)
    db.commit()
    return True


def _token_is_stale(account: WallabagAccount) -> bool:
    if not account.access_token:
        return True
    if account.token_expires_at is None:
        return True
    exp = account.token_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp - datetime.now(UTC) < timedelta(seconds=_REFRESH_LEEWAY_SECONDS)


def send_url(
    db: Session, account: WallabagAccount, link: str,
    tags: list[str] | None = None,
) -> bool:
    """POST link to /api/entries.json. Refreshes token first if stale;
    retries once on 401. Returns True on 2xx, False otherwise."""
    if _token_is_stale(account) and not _refresh(db, account):
        return False

    endpoint = f"{_normalize_instance(account.instance_url)}/api/entries.json"
    body = {"url": link}
    if tags:
        body["tags"] = ",".join(tags)

    def _post(token: str) -> httpx.Response:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            return client.post(endpoint, json=body,
                               headers={"Authorization": f"Bearer {token}"})

    try:
        r = _post(account.access_token)
        if r.status_code == 401 and _refresh(db, account):
            r = _post(account.access_token)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("wallabag: send_url failed for user %s: %s",
                    account.user_id, exc)
        return False
    return True


def send_url_safe(user_id: int, link: str, tags: list[str] | None = None) -> None:
    """Background-task entry point: opens its own DB session, no-ops if
    the user has no enabled Wallabag account. Never raises."""
    try:
        with Session(engine) as db:
            account = db.exec(
                select(WallabagAccount).where(WallabagAccount.user_id == user_id)
            ).first()
            if not account or not account.enabled or not account.access_token:
                return
            send_url(db, account, link, tags=tags)
    except Exception as exc:  # noqa: BLE001
        log.warning("wallabag: background dispatch crashed for user %s: %s",
                    user_id, exc)


def get_account(db: Session, user: User) -> WallabagAccount | None:
    return db.exec(
        select(WallabagAccount).where(WallabagAccount.user_id == user.id)
    ).first()
