"""ActivityPub — discovery-only outbox (0.11.17, aka the M6 milestone).

What this ships:
  - WebFinger:  /.well-known/webfinger?resource=acct:<handle>@<host>
  - Actor JSON: content-negotiated at /u/{handle} (Accept:
                application/activity+json OR application/ld+json)
                and also at explicit /u/{handle}/actor.json.
  - Outbox:     /u/{handle}/outbox — an OrderedCollection of
                Create{Note} activities derived from the user's
                Activity log (bookmarked + posted kinds, which map
                cleanly to AS 2.0 Notes).

What this DELIBERATELY doesn't ship:
  - Inbox / inbound Follow acceptance
  - HTTP Signature verification of remote actors
  - Outbound delivery (no queue, no push)
  - Non-Note activity kinds (Subscribe, Fork — those need a custom
    vocab and Mastodon won't render them meaningfully)

Result: a Mastodon user can paste `@handle@rssfriends.com` into their
search, see your profile + a feed of your recent activity, but can't
follow you into their timeline. That's the "M6 · Discoverable" scope.
"""

from __future__ import annotations

import logging
from datetime import UTC
from urllib.parse import unquote, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from . import config
from .db import Activity, Feed, FeedItem, User, get_session

log = logging.getLogger(__name__)

router = APIRouter()

AP_CONTENT_TYPE = "application/activity+json; charset=utf-8"

# JSON-LD @context the AS 2.0 spec expects for actors + activities.
AP_CONTEXT: list = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
]

# Cap on the outbox page size. Mastodon requests paginated collections;
# we return everything in one page for now since our activity volume
# is tiny — bump this and add ?page= if it ever matters.
OUTBOX_LIMIT = 40


# ─── Helpers ───────────────────────────────────────────────────────────


def _base() -> str:
    """Instance base URL, no trailing slash."""
    return config.BASE_URL.rstrip("/")


def _actor_id(handle: str) -> str:
    return f"{_base()}/u/{handle}"


def _instance_host() -> str:
    """The host portion of BASE_URL — used to form acct:<handle>@<host>."""
    return urlparse(config.BASE_URL).netloc or "localhost"


def wants_activitypub(request: Request) -> bool:
    """Content negotiation: does this request want AS 2.0 JSON?"""
    accept = (request.headers.get("accept") or "").lower()
    return (
        "application/activity+json" in accept
        or "application/ld+json" in accept
    )


def _generate_keypair() -> tuple[str, str]:
    """Fresh 2048-bit RSA keypair, PEM-encoded (PKCS#8 private, SubjectPublicKeyInfo public)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pub


def ensure_user_keypair(db: Session, user: User) -> User:
    """Lazily mint an AP keypair on first Actor request. Idempotent."""
    if user.ap_private_key and user.ap_public_key:
        return user
    priv, pub = _generate_keypair()
    user.ap_private_key = priv
    user.ap_public_key = pub
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _actor_document(user: User) -> dict:
    """AS 2.0 Person JSON for a rssFriends user. Callers must have
    already called ensure_user_keypair(db, user)."""
    actor_id = _actor_id(user.handle)
    return {
        "@context": AP_CONTEXT,
        "id": actor_id,
        "type": "Person",
        "preferredUsername": user.handle,
        "name": user.handle,
        "summary": (
            "rssFriends radar — RSS reading history + bookmarks. "
            "Follow at "
            f"{_base()}/u/{user.handle}"
        ),
        "url": actor_id,
        "manuallyApprovesFollowers": user.visibility == "private",
        # Inbox is REQUIRED on Person; we point at a placeholder that
        # currently returns 501. That's honest — Mastodon will render
        # the profile fine, and if it ever tries to POST a Follow it
        # gets a clean rejection instead of a 404.
        "inbox": f"{actor_id}/inbox",
        "outbox": f"{actor_id}/outbox",
        "followers": f"{actor_id}/followers",
        "following": f"{actor_id}/following",
        "publicKey": {
            "id": f"{actor_id}#main-key",
            "owner": actor_id,
            "publicKeyPem": user.ap_public_key,
        },
    }


def _activity_to_note(act: Activity, user: User,
                      feed: Feed | None, item: FeedItem | None) -> dict | None:
    """Convert a rssFriends Activity row to an AS 2.0 Create{Note}.

    Only two kinds map cleanly:
      - bookmarked (ref_type='item', item + feed lookup required)
      - posted     (ref_type='external_item', item + feed lookup required)

    Other kinds (subscribed / followed / forked / created_category)
    need a custom vocab and Mastodon would render them as empty. We
    return None for those so the caller filters them out."""
    if act.kind not in ("bookmarked", "posted"):
        return None
    if not item or not feed:
        return None

    ts = act.created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    ts_iso = ts.isoformat()

    actor_id = _actor_id(user.handle)
    note_id = f"{_base()}/i/{item.id}"
    verb = "bookmarked" if act.kind == "bookmarked" else "posted"

    # Human-readable body. Keep it plain — Mastodon will render <a> and
    # <br/>, but the point is discovery, not rich reproduction.
    content_html = (
        f'<p><strong>{user.handle}</strong> {verb} '
        f'<a href="{item.link}">{item.title}</a> '
        f'from <em>{feed.title}</em>.</p>'
    )

    return {
        "id": f"{note_id}#activity-{act.id}",
        "type": "Create",
        "actor": actor_id,
        "published": ts_iso,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [f"{actor_id}/followers"],
        "object": {
            "id": note_id,
            "type": "Note",
            "attributedTo": actor_id,
            "published": ts_iso,
            "content": content_html,
            "url": item.link,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [f"{actor_id}/followers"],
        },
    }


# ─── Routes ────────────────────────────────────────────────────────────


@router.get("/.well-known/webfinger")
def webfinger(
    resource: str = Query(...),
    db: Session = Depends(get_session),
):
    """WebFinger — the first hop a remote server makes when a user
    types `@handle@rssfriends.com`. Returns a JRD pointing at the
    Actor URL."""
    # Format is `acct:<handle>@<host>`. Some clients URL-encode the @.
    r = unquote(resource)
    if not r.startswith("acct:"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only acct: resources are supported.")
    body = r[len("acct:"):]
    if "@" not in body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed acct: expected handle@host.")
    handle, host = body.rsplit("@", 1)

    if host.lower() != _instance_host().lower():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not on this instance.")

    user = db.exec(select(User).where(User.handle == handle)).first()
    if not user or user.visibility == "private":
        # Private users are invisible to WebFinger — same posture as
        # not existing at all.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")

    actor_id = _actor_id(user.handle)
    return JSONResponse(
        {
            "subject": f"acct:{user.handle}@{_instance_host()}",
            "aliases": [actor_id],
            "links": [
                {
                    "rel": "self",
                    "type": "application/activity+json",
                    "href": actor_id,
                },
                {
                    "rel": "http://webfinger.net/rel/profile-page",
                    "type": "text/html",
                    "href": actor_id,
                },
            ],
        },
        media_type="application/jrd+json; charset=utf-8",
    )


def render_actor_json(request: Request, db: Session, user: User) -> JSONResponse:
    """Shared path for both /u/{handle} (Accept-negotiated) and
    /u/{handle}/actor.json (explicit)."""
    user = ensure_user_keypair(db, user)
    doc = _actor_document(user)
    return JSONResponse(doc, media_type=AP_CONTENT_TYPE)


@router.get("/u/{handle}/actor.json")
def actor_json(
    handle: str,
    request: Request,
    db: Session = Depends(get_session),
):
    """Explicit AS 2.0 Actor endpoint. Same body you get from
    /u/{handle} with an Accept:activity+json header."""
    user = db.exec(select(User).where(User.handle == handle)).first()
    if not user or user.visibility == "private":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
    return render_actor_json(request, db, user)


@router.get("/u/{handle}/outbox")
def outbox(
    handle: str,
    request: Request,
    db: Session = Depends(get_session),
):
    """OrderedCollection of Create{Note} activities. Public users only.
    For MVP, returns everything in one page — activity volume is tiny.
    Bumps to paginated OrderedCollection when we outgrow it."""
    user = db.exec(select(User).where(User.handle == handle)).first()
    if not user or user.visibility == "private":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")

    # Pull the raw activity rows, join to items + feeds so we have
    # everything the note-mapper needs in one pass.
    rows = db.exec(
        select(Activity)
        .where(Activity.user_id == user.id)
        .where(Activity.kind.in_(("bookmarked", "posted")))
        .order_by(Activity.created_at.desc())
        .limit(OUTBOX_LIMIT)
    ).all()

    item_ids = {a.ref_id for a in rows if a.ref_id is not None}
    items_by_id: dict[int, FeedItem] = {}
    feeds_by_id: dict[int, Feed] = {}
    if item_ids:
        items = db.exec(select(FeedItem).where(FeedItem.id.in_(item_ids))).all()
        items_by_id = {i.id: i for i in items}
        feed_ids = {i.feed_id for i in items}
        if feed_ids:
            feeds = db.exec(select(Feed).where(Feed.id.in_(feed_ids))).all()
            feeds_by_id = {f.id: f for f in feeds}

    notes: list[dict] = []
    for act in rows:
        item = items_by_id.get(act.ref_id) if act.ref_id else None
        feed = feeds_by_id.get(item.feed_id) if item else None
        note = _activity_to_note(act, user, feed, item)
        if note:
            notes.append(note)

    actor_id = _actor_id(user.handle)
    body = {
        "@context": AP_CONTEXT,
        "id": f"{actor_id}/outbox",
        "type": "OrderedCollection",
        "totalItems": len(notes),
        "orderedItems": notes,
    }
    return JSONResponse(body, media_type=AP_CONTENT_TYPE)


@router.post("/u/{handle}/inbox")
def inbox_placeholder(handle: str):
    """Placeholder inbox — required to be advertised on the Actor doc
    (Mastodon crashes without it), but this instance is
    discovery-only. Anything POSTed here gets 501 Not Implemented so
    remote servers know to stop retrying."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "This instance is discovery-only — inbound federation is not accepted.",
    )
