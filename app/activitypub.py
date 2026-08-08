"""ActivityPub — WebFinger + Actor + Outbox + Inbox + delivery.

Covers milestones M6 (Discoverable, 0.11.17), M7 (Inbound follow,
0.11.18), M8 (Outbound delivery, 0.11.18-0.11.21).

What ships:
  - WebFinger:      /.well-known/webfinger?resource=acct:<handle>@<host>
  - Actor JSON:     content-negotiated at /u/{handle}, also explicit
                    /u/{handle}/actor.json.
  - Outbox:         /u/{handle}/outbox — OrderedCollection of
                    Create{Note} activities (bookmarked + posted
                    kinds map cleanly to AS 2.0 Notes).
  - Inbox:          POST /u/{handle}/inbox accepts Follow +
                    Undo{Follow}. HTTP-Signature verified via the
                    signer's advertised publicKeyPem (fetched with
                    a signed GET so Mastodon "authorized fetch" mode
                    works). Accept{Follow} POSTed back on success.
  - Followers:      GET /u/{handle}/followers (content-negotiated
                    with the HTML follower page owned by follow.py).
  - Delivery:       every bookmark / cross-post fires a background
                    thread that HTTP-signs a Create{Note} and POSTs
                    it to each remote follower's inbox. Snapshot is
                    taken in the caller's session to avoid racing
                    the outer commit.

Still missing (the M9 wish-list):
  - Delete / Update propagation
  - Blocks + Announce
  - Custom radar:Subscribe / radar:Fork vocab
  - Outbound Follow (rssFriends → Mastodon)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session, select

from . import config
from .db import (
    Activity,
    Feed,
    FeedItem,
    RemoteActor,
    RemoteFollow,
    User,
    get_session,
)

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
            "Follow me here and my bookmarks + cross-posts land on your "
            f"timeline. Web view: {_base()}/u/{user.handle}"
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


def _resolve_ap_targets(db: Session, act: Activity) -> tuple[Feed | None, FeedItem | None]:
    """Look up the Feed + FeedItem context for building an AS 2.0 Note
    from an Activity row. Different kinds ref different tables:
      - bookmarked / posted → ref_id is a FeedItem
      - subscribed         → ref_id is a Feed (no item)
    Returns (None, None) for kinds we don't federate as Notes."""
    if act.kind in ("bookmarked", "posted"):
        item = db.get(FeedItem, act.ref_id) if act.ref_id else None
        feed = db.get(Feed, item.feed_id) if item else None
        return (feed, item)
    if act.kind == "subscribed":
        feed = db.get(Feed, act.ref_id) if act.ref_id else None
        return (feed, None)
    return (None, None)


def _activity_to_note(act: Activity, user: User,
                      feed: Feed | None, item: FeedItem | None) -> dict | None:
    """Convert a rssFriends Activity row to an AS 2.0 Create{Note}.

    Kinds mapped:
      - bookmarked (item + feed required)
      - posted     (item + feed required, from personal-feed cross-post)
      - subscribed (feed only — 0.11.23; renders "started following …")

    Followed / forked / created_category don't map — they'd need a
    custom vocab Mastodon can't render. Returns None so the caller
    filters them out."""
    if act.kind not in ("bookmarked", "posted", "subscribed"):
        return None

    ts = act.created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    ts_iso = ts.isoformat()

    actor_id = _actor_id(user.handle)

    if act.kind == "subscribed":
        if not feed:
            return None
        target_url = feed.site_url or feed.url
        content_html = (
            f'<p><strong>{user.handle}</strong> is now following '
            f'<a href="{target_url}"><em>{feed.title}</em></a> '
            f'on rssFriends.</p>'
        )
        note_url = target_url
    else:
        if not item or not feed:
            return None
        verb = "bookmarked" if act.kind == "bookmarked" else "posted"
        # Human-readable body. Keep it plain — Mastodon will render <a>
        # and <br/>, but the point is discovery, not rich reproduction.
        content_html = (
            f'<p><strong>{user.handle}</strong> {verb} '
            f'<a href="{item.link}">{item.title}</a> '
            f'from <em>{feed.title}</em>.</p>'
        )
        note_url = item.link

    # 0.11.21: activities delivered to a remote inbox must carry the
    # AP @context at the top level — Mastodon silently drops Create
    # wrappers without it (they return 202 anyway, so the drop is
    # invisible unless you dig into their sidekiq errors). Also give
    # the Create + Note distinct, dereferenceable-looking IDs.
    return {
        "@context": AP_CONTEXT,
        "id": f"{_base()}/ap/activities/{act.id}",
        "type": "Create",
        "actor": actor_id,
        "published": ts_iso,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [f"{actor_id}/followers"],
        "object": {
            "id": f"{_base()}/ap/notes/{act.id}",
            "type": "Note",
            "attributedTo": actor_id,
            "published": ts_iso,
            "content": content_html,
            "url": note_url,
            "sensitive": False,
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
        .where(Activity.kind.in_(("bookmarked", "posted", "subscribed")))
        .order_by(Activity.created_at.desc())
        .limit(OUTBOX_LIMIT)
    ).all()

    notes: list[dict] = []
    for act in rows:
        feed, item = _resolve_ap_targets(db, act)
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


# ─── HTTP Signature (draft-cavage-http-signatures-12, what Mastodon uses) ─


def _b64_of_sha256(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def _rfc7231_date() -> str:
    """HTTP-date per RFC 7231, required by draft-cavage."""
    return datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _parse_signature_header(sig_header: str) -> dict:
    """Parse a draft-cavage Signature: keyId="…",algorithm="…",… string."""
    out: dict[str, str] = {}
    # naive but sufficient: split on commas outside of quotes
    for part in re.findall(r'([a-zA-Z]+)="((?:\\.|[^"])*)"', sig_header):
        out[part[0]] = part[1]
    return out


def _canonical_signing_string(method: str, path: str,
                              headers: dict[str, str],
                              signed_headers: list[str]) -> str:
    """Rebuild the string that was signed. Header names are lowercase;
    (request-target) is the pseudo-header method + path."""
    lines = []
    for h in signed_headers:
        if h == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path}")
        else:
            v = headers.get(h.lower(), "")
            lines.append(f"{h.lower()}: {v}")
    return "\n".join(lines)


def _verify_http_signature(request: Request, body: bytes,
                           public_pem: str) -> bool:
    """Verify the request's HTTP Signature against the provided public
    key. Enforces: Signature header present, Digest header matches
    body (if present in signed set), Date within 24h window."""
    sig_hdr = request.headers.get("signature")
    if not sig_hdr:
        return False
    parts = _parse_signature_header(sig_hdr)
    signed = (parts.get("headers") or "date").split(" ")
    signature_b64 = parts.get("signature", "")
    if not signature_b64:
        return False

    # Reject the request outright if Digest doesn't match body.
    if "digest" in signed and body:
        expected = "SHA-256=" + _b64_of_sha256(body)
        if request.headers.get("digest") != expected:
            return False

    # Reject if Date is more than 24h off — kills long-replay windows.
    if "date" in signed:
        date_hdr = request.headers.get("date")
        if date_hdr:
            try:
                sent = datetime.strptime(date_hdr, "%a, %d %b %Y %H:%M:%S GMT")
                sent = sent.replace(tzinfo=UTC)
                delta = abs((datetime.now(UTC) - sent).total_seconds())
                if delta > 86400:
                    return False
            except ValueError:
                return False

    # Rebuild + verify.
    method = request.method
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    headers = {k.lower(): v for k, v in request.headers.items()}
    signing_string = _canonical_signing_string(method, path, headers, signed)

    try:
        pub = serialization.load_pem_public_key(public_pem.encode("utf-8"))
        pub.verify(
            base64.b64decode(signature_b64),
            signing_string.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError):
        return False


def _sign_and_post(private_pem: str, key_id: str,
                   target_url: str, body: bytes) -> httpx.Response:
    """Send `body` as an HTTP-signed POST to `target_url` (a remote
    inbox). Signature covers (request-target) host date digest."""
    parsed = urlparse(target_url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    date = _rfc7231_date()
    digest = "SHA-256=" + _b64_of_sha256(body)
    headers_to_sign = ["(request-target)", "host", "date", "digest"]
    header_values = {
        "(request-target)": f"post {path}",
        "host": parsed.netloc,
        "date": date,
        "digest": digest,
    }
    signing_string = "\n".join(f"{h}: {header_values[h]}" for h in headers_to_sign)
    priv = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    signature = priv.sign(
        signing_string.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    signature_b64 = base64.b64encode(signature).decode("ascii")
    sig_header = (
        f'keyId="{key_id}",algorithm="rsa-sha256",'
        f'headers="(request-target) host date digest",'
        f'signature="{signature_b64}"'
    )
    return httpx.post(
        target_url,
        content=body,
        headers={
            "Content-Type": AP_CONTENT_TYPE,
            "Accept": "application/activity+json",
            "Host": parsed.netloc,
            "Date": date,
            "Digest": digest,
            "Signature": sig_header,
            "User-Agent": f"rssFriends/0.11 (+{_base()})",
        },
        timeout=15,
        follow_redirects=True,
    )


# ─── Remote-actor lookup + cache ───────────────────────────────────────


def _signed_get(actor_url: str, as_user: User) -> httpx.Response:
    """HTTP-signed GET. Needed for instances running Mastodon's
    "authorized fetch" (secure) mode — an unsigned actor GET returns
    401. We sign on behalf of the recipient user (the one being
    followed) with their own keypair."""
    parsed = urlparse(actor_url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    date = _rfc7231_date()
    signed = ["(request-target)", "host", "date"]
    hvals = {
        "(request-target)": f"get {path}",
        "host": parsed.netloc,
        "date": date,
    }
    signing_string = "\n".join(f"{h}: {hvals[h]}" for h in signed)
    priv = serialization.load_pem_private_key(
        as_user.ap_private_key.encode("utf-8"), password=None,
    )
    sig = base64.b64encode(priv.sign(
        signing_string.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )).decode("ascii")
    key_id = f"{_actor_id(as_user.handle)}#main-key"
    sig_hdr = (
        f'keyId="{key_id}",algorithm="rsa-sha256",'
        f'headers="(request-target) host date",'
        f'signature="{sig}"'
    )
    return httpx.get(
        actor_url,
        headers={
            "Accept": "application/activity+json",
            "Host": parsed.netloc,
            "Date": date,
            "Signature": sig_hdr,
            "User-Agent": f"rssFriends/0.11 (+{_base()})",
        },
        timeout=10,
        follow_redirects=True,
    )


def _fetch_remote_actor_doc(actor_url: str, as_user: User | None = None) -> dict:
    """Fetch an actor's JSON. If `as_user` is provided (and has a
    keypair), the request is HTTP-signed on their behalf — required
    for Mastodon instances running "authorized fetch" mode. Falls
    back to an unsigned GET when `as_user` is None (e.g. keyless
    contexts)."""
    if as_user is not None and as_user.ap_private_key:
        r = _signed_get(actor_url, as_user)
    else:
        r = httpx.get(
            actor_url,
            headers={"Accept": "application/activity+json"},
            timeout=10,
            follow_redirects=True,
        )
    r.raise_for_status()
    return r.json()


def get_or_cache_remote_actor(db: Session, actor_url: str,
                              as_user: User | None = None) -> RemoteActor:
    """Find-or-create for a RemoteActor row. Refetches once every 7
    days so key rotations propagate without a manual purge."""
    existing = db.exec(
        select(RemoteActor).where(RemoteActor.actor_url == actor_url)
    ).first()
    fresh_window = 7 * 86400  # 7 days
    if existing:
        fetched = existing.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - fetched).total_seconds()
        if age < fresh_window:
            return existing

    try:
        doc = _fetch_remote_actor_doc(actor_url, as_user=as_user)
    except (httpx.HTTPError, ValueError) as e:
        if existing:
            return existing  # stale is better than raising
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"can't fetch remote actor: {e}",
        ) from e

    pk = doc.get("publicKey") or {}
    pem = pk.get("publicKeyPem") or ""
    if not pem:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "remote actor has no publicKey — can't federate.",
        )
    endpoints = doc.get("endpoints") or {}
    inbox = endpoints.get("sharedInbox") or doc.get("inbox") or ""
    if not inbox:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "remote actor advertises no inbox.",
        )

    if existing:
        existing.public_key_pem = pem
        existing.inbox_url = inbox
        existing.key_id = pk.get("id") or existing.key_id
        existing.preferred_username = doc.get("preferredUsername", "") or ""
        existing.display_name = doc.get("name", "") or ""
        existing.fetched_at = datetime.now(UTC)
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    ra = RemoteActor(
        actor_url=actor_url,
        inbox_url=inbox,
        public_key_pem=pem,
        key_id=pk.get("id") or "",
        preferred_username=doc.get("preferredUsername", "") or "",
        display_name=doc.get("name", "") or "",
    )
    db.add(ra)
    db.commit()
    db.refresh(ra)
    return ra


# ─── Inbox: parse Follow / Undo(Follow) ────────────────────────────────


def _key_id_owner(key_id: str) -> str:
    """A keyId is usually "https://actor#main-key"; the actor URL is
    everything before the '#'."""
    return key_id.split("#", 1)[0]


BACKFILL_LIMIT = 20  # 0.11.22: how many past Notes we push on follow


def _backfill_history_to_follower(
    private_pem: str, key_id: str, inbox_url: str,
    payloads: list[bytes],
) -> None:
    """Deliver `payloads` (already-built Note JSON) to `inbox_url` one
    at a time. Each Note carries its original `published` timestamp,
    so Mastodon slots them chronologically in the follower's timeline
    (they don't flood the "now" view). Runs in a background thread —
    fires-and-forgets errors."""
    for p in payloads:
        _deliver_one(private_pem, key_id, inbox_url, p)


def _enqueue_history_backfill(db: Session, our_user: User,
                              remote_actor: RemoteActor) -> None:
    """Snapshot recent Notes and hand them to a delivery thread. Same
    "no DB access in the thread" pattern as new-activity delivery."""
    rows = db.exec(
        select(Activity)
        .where(Activity.user_id == our_user.id)
        .where(Activity.kind.in_(("bookmarked", "posted", "subscribed")))
        .order_by(Activity.created_at.desc())
        .limit(BACKFILL_LIMIT)
    ).all()
    if not rows:
        return

    payloads: list[bytes] = []
    # Deliver oldest→newest so the timeline reads in the expected order
    # if Mastodon happens to render by delivery time rather than
    # `published` (belt+braces).
    for act in reversed(rows):
        feed, item = _resolve_ap_targets(db, act)
        note = _activity_to_note(act, our_user, feed, item)
        if note:
            payloads.append(json.dumps(note).encode("utf-8"))
    if not payloads:
        return

    key_id = f"{_actor_id(our_user.handle)}#main-key"
    log.warning("AP backfill: sending %d historical Note(s) to %s",
                len(payloads), remote_actor.inbox_url)
    t = threading.Thread(
        target=_backfill_history_to_follower,
        args=(our_user.ap_private_key, key_id, remote_actor.inbox_url, payloads),
        daemon=True,
    )
    t.start()


def _handle_follow(db: Session, activity: dict, our_user: User,
                   remote_actor: RemoteActor) -> None:
    """Persist the Follow, POST an Accept{Follow}, then backfill the
    follower's timeline with our recent historical Notes so their
    profile view of us isn't empty."""
    follow_id = activity.get("id") or ""

    existing = db.exec(
        select(RemoteFollow)
        .where(RemoteFollow.remote_actor_id == remote_actor.id)
        .where(RemoteFollow.user_id == our_user.id)
    ).first()
    if not existing:
        db.add(RemoteFollow(
            remote_actor_id=remote_actor.id,
            user_id=our_user.id,
            follow_activity_id=follow_id,
        ))
        db.commit()

    # Mint + POST Accept.
    actor_id = _actor_id(our_user.handle)
    accept = {
        "@context": AP_CONTEXT,
        "id": f"{actor_id}#accepts/{follow_id.rsplit('/', 1)[-1] or 'follow'}",
        "type": "Accept",
        "actor": actor_id,
        "object": activity,
    }
    try:
        _sign_and_post(
            our_user.ap_private_key,
            f"{actor_id}#main-key",
            remote_actor.inbox_url,
            json.dumps(accept).encode("utf-8"),
        )
    except httpx.HTTPError as e:
        # We accepted their follow in our DB; delivery of Accept failed.
        # They'll see us as "pending" but our next Create delivery will
        # still land, so it self-heals. Log and move on.
        log.warning("Accept delivery failed to %s: %s", remote_actor.inbox_url, e)

    # 0.11.24: backfill on every accepted Follow (was: only when we
    # didn't already have a RemoteFollow row). Reason: a handle rename
    # leaves the old row in place with the same user_id, so a fresh
    # Follow of the new handle would skip backfill and show an empty
    # profile — surprising and wrong. If a remote server double-follows
    # without an intervening Undo (Mastodon's client normally prevents
    # this), yes, they get the same 20 Notes twice; acceptable trade.
    _enqueue_history_backfill(db, our_user, remote_actor)


def _handle_undo(db: Session, activity: dict, our_user: User,
                 remote_actor: RemoteActor) -> None:
    """Undo{Follow}: drop the RemoteFollow row so they stop receiving
    our Create deliveries."""
    inner = activity.get("object") or {}
    if isinstance(inner, str):
        return  # bare Undo of a URL — we can't tell what was undone
    if inner.get("type") != "Follow":
        return
    row = db.exec(
        select(RemoteFollow)
        .where(RemoteFollow.remote_actor_id == remote_actor.id)
        .where(RemoteFollow.user_id == our_user.id)
    ).first()
    if row:
        db.delete(row)
        db.commit()


@router.post("/u/{handle}/ap/backfill")
def admin_backfill(
    handle: str,
    request: Request,
    db: Session = Depends(get_session),
):
    """0.11.24: owner-only manual re-delivery of the last 20 Notes to
    every existing remote follower. Useful when the automatic
    on-follow backfill was skipped (e.g. renames prior to this
    release, or restoring from backup)."""
    from .auth import current_user
    viewer = current_user(request, db)
    if not viewer or viewer.handle != handle:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Owner-only: sign in as this handle.")
    user = db.exec(select(User).where(User.handle == handle)).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
    user = ensure_user_keypair(db, user)
    follows = db.exec(
        select(RemoteFollow, RemoteActor)
        .join(RemoteActor, RemoteActor.id == RemoteFollow.remote_actor_id)
        .where(RemoteFollow.user_id == user.id)
    ).all()
    for _rf, ra in follows:
        _enqueue_history_backfill(db, user, ra)
    return JSONResponse(
        {"queued_backfill_to": [ra.actor_url for _rf, ra in follows]},
    )


@router.post("/u/{handle}/inbox")
async def inbox(
    handle: str,
    request: Request,
    db: Session = Depends(get_session),
):
    """Real inbox — accepts Follow + Undo{Follow}. Every message must
    be HTTP-signed by an actor whose public key we can fetch. Other
    activity types (Create, Delete, Like, Announce) return 202 Accepted
    so remote servers stop retrying, but we don't act on them yet."""
    # 0.11.24: resolve via HandleAlias too so Undo/Follow addressed
    # to a retired handle (after a rename) still reach the right user.
    from .public import resolve_handle
    our_user = resolve_handle(db, handle)
    if not our_user or our_user.visibility == "private":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")

    # Make sure the recipient has a keypair — we need it to sign the
    # actor-fetch GET (Mastodon's authorized-fetch mode) and later to
    # sign the Accept POST.
    our_user = ensure_user_keypair(db, our_user)

    body = await request.body()
    try:
        activity = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body isn't valid JSON.") from None

    # Extract the actor of the message and verify the HTTP Signature
    # against their advertised public key. Reject anything unsigned.
    sender_url = activity.get("actor")
    if isinstance(sender_url, dict):
        sender_url = sender_url.get("id")
    if not sender_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing actor.")

    # keyId in the Signature must belong to the same actor — prevents
    # Actor A signing a message that claims to come from Actor B.
    sig_hdr = request.headers.get("signature") or ""
    key_id = _parse_signature_header(sig_hdr).get("keyId") or ""
    if _key_id_owner(key_id) != sender_url:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Signature keyId doesn't match message actor.",
        )

    remote_actor = get_or_cache_remote_actor(db, sender_url, as_user=our_user)
    if not _verify_http_signature(request, body, remote_actor.public_key_pem):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "HTTP Signature verification failed.",
        )

    kind = activity.get("type")
    if kind == "Follow":
        _handle_follow(db, activity, our_user, remote_actor)
    elif kind == "Undo":
        _handle_undo(db, activity, our_user, remote_actor)
    # else: 202-accept-and-drop for MVP scope.

    return Response(status_code=status.HTTP_202_ACCEPTED)


# ─── Followers collection ─────────────────────────────────────────────


def followers(handle: str, db: Session = Depends(get_session)):
    """OrderedCollection of remote follower actor URLs. Mastodon
    reads this to show the follower count/list on the profile card.

    Not registered as a route directly — follow.py owns the HTML
    /u/{handle}/followers URL and delegates here via content-neg
    when the caller asks for application/activity+json. That avoids
    the router collision that bit us when both files claimed the
    same path (0.11.18).
    """
    user = db.exec(select(User).where(User.handle == handle)).first()
    if not user or user.visibility == "private":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
    rows = db.exec(
        select(RemoteFollow, RemoteActor)
        .join(RemoteActor, RemoteActor.id == RemoteFollow.remote_actor_id)
        .where(RemoteFollow.user_id == user.id)
    ).all()
    urls = [ra.actor_url for _rf, ra in rows]
    actor_id = _actor_id(user.handle)
    return JSONResponse(
        {
            "@context": AP_CONTEXT,
            "id": f"{actor_id}/followers",
            "type": "OrderedCollection",
            "totalItems": len(urls),
            "orderedItems": urls,
        },
        media_type=AP_CONTENT_TYPE,
    )


# ─── Outbound delivery on new activity ────────────────────────────────


def _deliver_one(private_pem: str, key_id: str,
                 inbox_url: str, activity_json: bytes) -> None:
    """Fire-and-forget POST wrapped so exceptions land in logs, not
    the caller. Called from the delivery thread. Uses log.warning
    (not log.info) so results are visible under uvicorn's default
    log level."""
    try:
        r = _sign_and_post(private_pem, key_id, inbox_url, activity_json)
        if r.status_code >= 400:
            log.warning("AP delivery to %s → %d: %s",
                        inbox_url, r.status_code, r.text[:200])
        else:
            log.warning("AP delivery to %s ok (%d)", inbox_url, r.status_code)
    except httpx.HTTPError as e:
        log.warning("AP delivery to %s errored: %s", inbox_url, e)


def _deliver_payload(private_pem: str, key_id: str,
                     payload: bytes, targets: list[str]) -> None:
    """Thread body — every arg is a plain value (no DB session). Loops
    through remote inbox URLs and fires a signed POST at each."""
    for inbox_url in targets:
        _deliver_one(private_pem, key_id, inbox_url, payload)


def enqueue_activity_delete(db: Session, user: User,
                            activity_row: Activity) -> None:
    """Called from the /activity/{id} DELETE endpoint (0.11.23).
    Pushes a signed Delete{Tombstone} to every remote follower so
    the Note we previously delivered disappears from their timeline.
    Snapshots the follower list in the caller's session (same
    no-DB-in-thread pattern as new-note delivery)."""
    if not user.ap_private_key:
        return
    # We can only delete Notes we would have sent — mirror the
    # emission filter exactly so we don't send phantom Deletes for
    # kinds we never federated as Notes in the first place.
    if activity_row.kind not in ("bookmarked", "posted", "subscribed"):
        return
    follows = db.exec(
        select(RemoteFollow, RemoteActor)
        .join(RemoteActor, RemoteActor.id == RemoteFollow.remote_actor_id)
        .where(RemoteFollow.user_id == user.id)
    ).all()
    inboxes = [ra.inbox_url for _rf, ra in follows]
    if not inboxes:
        return

    actor_id = _actor_id(user.handle)
    note_id = f"{_base()}/ap/notes/{activity_row.id}"
    delete_activity = {
        "@context": AP_CONTEXT,
        "id": f"{_base()}/ap/activities/{activity_row.id}/delete",
        "type": "Delete",
        "actor": actor_id,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [f"{actor_id}/followers"],
        "object": {
            "id": note_id,
            "type": "Tombstone",
        },
    }
    payload = json.dumps(delete_activity).encode("utf-8")
    key_id = f"{actor_id}#main-key"
    log.warning("AP delete: notifying %d follower(s) of note deletion for user=%s",
                len(inboxes), user.handle)
    t = threading.Thread(
        target=_deliver_payload,
        args=(user.ap_private_key, key_id, payload, inboxes),
        daemon=True,
    )
    t.start()


def enqueue_activity_delivery(db: Session, user: User,
                              activity_row: Activity) -> None:
    """Called from activity.record() while the caller's session is
    still open. Snapshots the note payload + follower inbox list from
    the current (uncommitted) session state, then spawns a thread
    that does the network I/O with plain-data-only inputs — no DB
    access in the thread, so there's no race between the delivery
    thread and the outer transaction commit.

    Bug we're avoiding (0.11.19 → 0.11.20): before this refactor, the
    thread opened a fresh Session and queried by activity_id. Since
    the outer transaction hadn't committed yet, the thread's snapshot
    saw the Activity row as non-existent and silently returned.
    Bookmarks landed in the DB but never delivered. Passing the
    payload directly bypasses that race entirely."""
    if not user.ap_private_key:
        return
    if activity_row.kind not in ("bookmarked", "posted", "subscribed"):
        return

    feed, item = _resolve_ap_targets(db, activity_row)
    note = _activity_to_note(activity_row, user, feed, item)
    if not note:
        return

    follows = db.exec(
        select(RemoteFollow, RemoteActor)
        .join(RemoteActor, RemoteActor.id == RemoteFollow.remote_actor_id)
        .where(RemoteFollow.user_id == user.id)
    ).all()
    inboxes = [ra.inbox_url for _rf, ra in follows]
    if not inboxes:
        return

    payload = json.dumps(note).encode("utf-8")
    key_id = f"{_actor_id(user.handle)}#main-key"
    log.warning("AP delivery: enqueuing to %d follower(s) for user=%s",
                len(inboxes), user.handle)
    t = threading.Thread(
        target=_deliver_payload,
        args=(user.ap_private_key, key_id, payload, inboxes),
        daemon=True,
    )
    t.start()
