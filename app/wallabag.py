"""Wallabag OAuth2 client + bookmark dispatcher.

Wallabag's public OAuth API only supports grant_type=password (see
https://doc.wallabag.org/developer/api/oauth/) — there is no
authorization-code endpoint. So the user pastes their instance URL +
client_id/client_secret (minted at {instance}/developer/client/create)
AND their Wallabag username + password. We exchange those for an
access_token + refresh_token pair and store all four fields.

When a user bookmarks an item and has an enabled WallabagAccount, we
enqueue a background task that POSTs the item's URL to their
/api/entries.json. Errors are logged and swallowed — the bookmark
row itself is the source of truth; Wallabag delivery is best-effort.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlmodel import Session, select

from .crypto import decrypt, encrypt
from .db import User, WallabagAccount, engine

log = logging.getLogger(__name__)

# Give ourselves a small buffer before token expiry so refreshes happen
# proactively instead of racing the first 401.
_REFRESH_LEEWAY_SECONDS = 60
_HTTP_TIMEOUT = 10.0


def _normalize_instance(url: str) -> str:
    return url.strip().rstrip("/")


def _store_tokens(db: Session, account: WallabagAccount, payload: dict) -> None:
    access = payload.get("access_token") or ""
    refresh = payload.get("refresh_token") or ""
    expires_in = int(payload.get("expires_in") or 0)
    account.access_token = encrypt(access)
    if refresh:
        account.refresh_token = encrypt(refresh)
    account.token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    account.enabled = True
    account.updated_at = datetime.now(UTC)
    db.add(account)


def store_credentials(
    account: WallabagAccount, *, client_secret: str, username: str, password: str,
) -> None:
    """Encrypt then store the user-supplied credentials on the row.
    `client_id` and `instance_url` are kept in plaintext — the ID is
    equivalent to a public identifier once combined with the secret,
    and the instance URL isn't a secret."""
    account.client_secret = encrypt(client_secret)
    account.username = username  # plaintext — needed to render form + not a secret
    account.password = encrypt(password)


def password_grant(db: Session, account: WallabagAccount) -> bool:
    """Exchange the stored (username, password, client_id, client_secret)
    for a token pair via grant_type=password.

    Returns True on success. On failure logs and returns False."""
    url = f"{_normalize_instance(account.instance_url)}/oauth/v2/token"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.post(url, data={
                "grant_type": "password",
                "client_id": account.client_id,
                "client_secret": decrypt(account.client_secret),
                "username": account.username,
                "password": decrypt(account.password),
            })
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("wallabag: password_grant failed for user %s: %s",
                    account.user_id, exc)
        return False
    _store_tokens(db, account, payload)
    db.commit()
    return True


def _refresh(db: Session, account: WallabagAccount) -> bool:
    """Try refresh_token grant; fall back to a fresh password_grant if the
    refresh token is missing or rejected. Password fallback matters
    because Wallabag rotates refresh tokens and some instances return
    non-standard errors that we don't want to distinguish."""
    url = f"{_normalize_instance(account.instance_url)}/oauth/v2/token"
    plain_refresh = decrypt(account.refresh_token)
    if plain_refresh:
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                r = client.post(url, data={
                    "grant_type": "refresh_token",
                    "client_id": account.client_id,
                    "client_secret": decrypt(account.client_secret),
                    "refresh_token": plain_refresh,
                })
                r.raise_for_status()
                payload = r.json()
            _store_tokens(db, account, payload)
            db.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            log.info("wallabag: refresh_token grant failed for user %s (%s);"
                     " falling back to password grant", account.user_id, exc)
    # Fall back to password grant.
    return password_grant(db, account)


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
        r = _post(decrypt(account.access_token))
        if r.status_code == 401 and _refresh(db, account):
            r = _post(decrypt(account.access_token))
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
