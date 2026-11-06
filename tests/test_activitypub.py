"""ActivityPub — discovery-only outbox (0.11.17, M6 scope).

Cover: WebFinger, Actor JSON via content-negotiation and explicit
route, Outbox mapping of Activity rows to AS 2.0 Notes, and the
501-inbox placeholder.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select


@pytest.fixture(autouse=True)
def _base_url_for_ap():
    """The AP module reads config.BASE_URL for actor + outbox IDs.
    Pin it to a stable value so we can assert on emitted URLs."""
    prev = os.environ.get("RSSFRIENDS_BASE_URL")
    os.environ["RSSFRIENDS_BASE_URL"] = "https://rssfriends.com"
    # Force config re-read since it's module-level.
    import importlib

    import app.config as cfg
    importlib.reload(cfg)
    import app.activitypub as ap
    importlib.reload(ap)
    yield
    if prev is None:
        del os.environ["RSSFRIENDS_BASE_URL"]
    else:
        os.environ["RSSFRIENDS_BASE_URL"] = prev
    importlib.reload(cfg)
    importlib.reload(ap)


def _client():
    import app.main
    return TestClient(app.main.app)


def _login(client, email):
    r = client.post("/login", data={"email": email})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    token = r.text[idx:end].split("token=", 1)[1]
    client.post("/auth/verify", data={"token": token}, follow_redirects=False)


# ─── WebFinger ─────────────────────────────────────────────────────────


def test_webfinger_resolves_public_user():
    c = _client()
    _login(c, "alice@example.com")
    r = c.get(
        "/.well-known/webfinger",
        params={"resource": "acct:alice@rssfriends.com"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["subject"] == "acct:alice@rssfriends.com"
    # Self link points to the actor and declares activity+json.
    self_link = next(lk for lk in body["links"] if lk["rel"] == "self")
    assert self_link["type"] == "application/activity+json"
    assert self_link["href"] == "https://rssfriends.com/u/alice"


def test_webfinger_404_for_unknown_user():
    r = _client().get(
        "/.well-known/webfinger",
        params={"resource": "acct:nobody@rssfriends.com"},
    )
    assert r.status_code == 404


def test_webfinger_404_for_off_instance_host():
    """A remote resolver asking about a different host must be told no —
    this instance only speaks for its own users."""
    r = _client().get(
        "/.well-known/webfinger",
        params={"resource": "acct:alice@some-other.example"},
    )
    assert r.status_code == 404


def test_webfinger_rejects_non_acct_resource():
    r = _client().get(
        "/.well-known/webfinger",
        params={"resource": "https://not-acct.example/alice"},
    )
    assert r.status_code == 400


def test_webfinger_hides_private_users():
    c = _client()
    _login(c, "alice@example.com")
    c.post("/settings/visibility", data={"visibility": "private"},
           follow_redirects=False)
    r = _client().get(
        "/.well-known/webfinger",
        params={"resource": "acct:alice@rssfriends.com"},
    )
    assert r.status_code == 404


# ─── Actor JSON ────────────────────────────────────────────────────────


def test_actor_json_via_content_negotiation():
    """Accept: application/activity+json on /u/{handle} returns AS 2.0
    JSON instead of HTML — that's how Mastodon fetches actor docs."""
    c = _client()
    _login(c, "alice@example.com")
    r = _client().get(
        "/u/alice",
        headers={"Accept": "application/activity+json"},
    )
    assert r.status_code == 200
    assert "application/activity+json" in r.headers["content-type"]
    body = r.json()
    assert body["type"] == "Person"
    assert body["preferredUsername"] == "alice"
    assert body["id"] == "https://rssfriends.com/u/alice"
    assert body["outbox"] == "https://rssfriends.com/u/alice/outbox"
    assert body["inbox"] == "https://rssfriends.com/u/alice/inbox"
    # publicKey block is required — Mastodon rejects actors without it.
    assert body["publicKey"]["owner"] == "https://rssfriends.com/u/alice"
    assert "BEGIN PUBLIC KEY" in body["publicKey"]["publicKeyPem"]
    # Sanity: @context declares the two vocabs we use.
    assert "https://www.w3.org/ns/activitystreams" in body["@context"]
    assert "https://w3id.org/security/v1" in body["@context"]


def test_actor_json_html_default_still_works():
    """No Accept header (or Accept: text/html) still returns the HTML page."""
    c = _client()
    _login(c, "alice@example.com")
    r = _client().get("/u/alice")   # default Accept in TestClient is */*
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<!doctype html" in r.text.lower()


def test_actor_json_explicit_route():
    c = _client()
    _login(c, "alice@example.com")
    r = _client().get("/u/alice/actor.json")
    assert r.status_code == 200
    assert "application/activity+json" in r.headers["content-type"]
    assert r.json()["type"] == "Person"


def test_actor_json_generates_keypair_lazily():
    """First Actor request mints an RSA keypair and persists it. Subsequent
    requests return the same key (idempotent)."""
    import app.db as db_mod

    c = _client()
    _login(c, "alice@example.com")

    # Baseline: no key yet.
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        assert alice.ap_private_key == ""
        assert alice.ap_public_key == ""

    r1 = _client().get("/u/alice/actor.json").json()
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        assert alice.ap_private_key.startswith("-----BEGIN PRIVATE KEY-----")
        assert alice.ap_public_key.startswith("-----BEGIN PUBLIC KEY-----")
        stored_pub = alice.ap_public_key

    # Second call returns the SAME key — never regenerated.
    r2 = _client().get("/u/alice/actor.json").json()
    assert r1["publicKey"]["publicKeyPem"] == r2["publicKey"]["publicKeyPem"]
    assert r2["publicKey"]["publicKeyPem"] == stored_pub


def test_actor_json_404_for_private_user():
    c = _client()
    _login(c, "alice@example.com")
    c.post("/settings/visibility", data={"visibility": "private"},
           follow_redirects=False)
    # Explicit route: 404.
    r = _client().get("/u/alice/actor.json")
    assert r.status_code == 404
    # Content-negotiated route: still HTML (private page), not AP.
    r2 = _client().get(
        "/u/alice",
        headers={"Accept": "application/activity+json"},
    )
    # Private radars serve HTML with 403; AP short-circuit is skipped.
    assert "activity+json" not in r2.headers.get("content-type", "")


# ─── Outbox ────────────────────────────────────────────────────────────


def test_outbox_empty_collection():
    c = _client()
    _login(c, "alice@example.com")
    r = _client().get("/u/alice/outbox")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "OrderedCollection"
    assert body["totalItems"] == 0
    assert body["orderedItems"] == []
    assert body["id"] == "https://rssfriends.com/u/alice/outbox"


def test_outbox_includes_bookmark_activities():
    """Bookmarked items should surface as Create{Note} activities."""
    import app.db as db_mod

    # Seed alice with a feed + item + bookmark.
    c = _client()
    _login(c, "alice@example.com")
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://ex.example/feed",
                           title="Example Blog",
                           site_url="https://ex.example")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="urn:1",
            title="Everyone should know SIMD",
            link="https://ex.example/simd",
        )
        s.add(item)
        s.commit()
        s.refresh(item)

    # Bookmarking through the normal endpoint also emits an Activity row.
    c.post(f"/items/{item.id}/bookmark")

    r = _client().get("/u/alice/outbox")
    assert r.status_code == 200
    body = r.json()
    assert body["totalItems"] == 1
    note = body["orderedItems"][0]
    assert note["type"] == "Create"
    assert note["actor"] == "https://rssfriends.com/u/alice"
    assert note["object"]["type"] == "Note"
    assert "Everyone should know SIMD" in note["object"]["content"]
    assert note["object"]["url"] == "https://ex.example/simd"
    # AS 2.0 Public addressing so Mastodon renders it as a public toot.
    assert "https://www.w3.org/ns/activitystreams#Public" in note["to"]


def test_outbox_404_for_private_user():
    c = _client()
    _login(c, "alice@example.com")
    c.post("/settings/visibility", data={"visibility": "private"},
           follow_redirects=False)
    r = _client().get("/u/alice/outbox")
    assert r.status_code == 404


# ─── Inbox placeholder ─────────────────────────────────────────────────


def test_inbox_rejects_unsigned_request():
    """0.11.18: inbox now really parses Follow — but only if the
    request is HTTP-signed by an actor whose key matches the message
    actor. Unsigned or missing-signature = 401."""
    c = _client()
    _login(c, "alice@example.com")
    r = _client().post(
        "/u/alice/inbox",
        content=json.dumps({
            "type": "Follow",
            "actor": "https://mastodon.example/users/foo",
            "object": "https://rssfriends.com/u/alice",
            "id": "https://mastodon.example/users/foo/follows/1",
        }),
        headers={"Content-Type": "application/activity+json"},
    )
    # No Signature header at all → keyId mismatch check trips first.
    assert r.status_code == 401


def test_inbox_rejects_bad_json():
    c = _client()
    _login(c, "alice@example.com")
    r = _client().post(
        "/u/alice/inbox",
        content=b"not json",
        headers={"Content-Type": "application/activity+json"},
    )
    assert r.status_code == 400


def test_inbox_key_id_owner_must_match_actor():
    """Prevents actor A signing a Follow that claims to be from actor B."""
    c = _client()
    _login(c, "alice@example.com")
    r = _client().post(
        "/u/alice/inbox",
        content=json.dumps({
            "type": "Follow",
            "actor": "https://mastodon.example/users/foo",   # A
            "id": "https://mastodon.example/users/foo/follows/1",
        }),
        # keyId points at a different actor (B) → 401
        headers={
            "Content-Type": "application/activity+json",
            "Signature": 'keyId="https://mastodon.example/users/EVIL#main-key",algorithm="rsa-sha256",headers="date",signature="AAAA"',
        },
    )
    assert r.status_code == 401


def test_inbox_logs_rejections(monkeypatch, caplog):
    """Every 401/502 from the inbox is logged with activity type,
    sender and recipient so prod rejections can be diagnosed."""
    import httpx

    import app.activitypub as ap

    c = _client()
    _login(c, "alice@example.com")

    def _gone(*_a, **_kw):
        raise httpx.HTTPStatusError(
            "410 Gone", request=httpx.Request("GET", "https://gone.example/users/x"),
            response=httpx.Response(410),
        )
    monkeypatch.setattr(ap, "_fetch_remote_actor_doc", _gone)

    with caplog.at_level("WARNING", logger="app.activitypub"):
        r = _client().post(
            "/u/alice/inbox",
            content=json.dumps({
                "type": "Delete",
                "actor": "https://gone.example/users/x",
                "id": "https://gone.example/users/x#delete",
            }),
            headers={
                "Content-Type": "application/activity+json",
                "Signature": 'keyId="https://gone.example/users/x#main-key",algorithm="rsa-sha256",headers="date",signature="AAAA"',
            },
        )
        assert r.status_code == 502

        r = _client().post(
            "/u/alice/inbox",
            content=json.dumps({
                "type": "Follow",
                "actor": "https://mastodon.example/users/foo",
                "id": "https://mastodon.example/users/foo/follows/1",
            }),
            headers={"Content-Type": "application/activity+json"},
        )
        assert r.status_code == 401

    lines = [rec.getMessage() for rec in caplog.records if "AP inbox reject" in rec.getMessage()]
    assert len(lines) == 2
    assert "502 type=Delete actor=https://gone.example/users/x to=alice" in lines[0]
    assert "410 Gone" in lines[0]
    assert "401 type=Follow actor=https://mastodon.example/users/foo to=alice" in lines[1]


def test_inbox_accepts_follow_with_valid_signature(monkeypatch):
    """End-to-end: mint a fake remote actor + keypair, sign a Follow,
    POST it, assert we recorded the RemoteFollow row and attempted an
    Accept delivery."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    import app.activitypub as ap
    import app.db as db_mod

    c = _client()
    _login(c, "alice@example.com")

    # Fake remote actor + keypair.
    remote_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    remote_pub_pem = remote_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    remote_priv_pem = remote_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    remote_actor_url = "https://mastodon.example/users/foo"

    # Stub actor fetch so we don't hit the network.
    def fake_fetch(url, as_user=None):
        assert url == remote_actor_url
        return {
            "id": remote_actor_url,
            "inbox": "https://mastodon.example/users/foo/inbox",
            "publicKey": {
                "id": f"{remote_actor_url}#main-key",
                "owner": remote_actor_url,
                "publicKeyPem": remote_pub_pem,
            },
            "preferredUsername": "foo",
            "name": "Foo",
        }
    monkeypatch.setattr(ap, "_fetch_remote_actor_doc", fake_fetch)

    # Stub outbound Accept delivery so we don't hit the network + can
    # observe that we tried to send.
    sent = []
    class _StubResp:
        status_code = 202
        text = ""

    def fake_post(private_pem, key_id, target_url, body):
        sent.append({"key_id": key_id, "url": target_url, "body": body})
        return _StubResp()
    monkeypatch.setattr(ap, "_sign_and_post", fake_post)

    # Sign the Follow with the remote key.
    follow = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{remote_actor_url}/follows/1",
        "type": "Follow",
        "actor": remote_actor_url,
        "object": "https://rssfriends.com/u/alice",
    }
    body = json.dumps(follow).encode("utf-8")
    date = ap._rfc7231_date()
    digest = "SHA-256=" + ap._b64_of_sha256(body)
    signing_string = (
        f"(request-target): post /u/alice/inbox\n"
        f"host: testserver\n"
        f"date: {date}\n"
        f"digest: {digest}"
    )
    from cryptography.hazmat.primitives import hashes as _h
    from cryptography.hazmat.primitives.asymmetric import padding as _p
    priv = serialization.load_pem_private_key(remote_priv_pem.encode(), password=None)
    sig_bytes = priv.sign(signing_string.encode(), _p.PKCS1v15(), _h.SHA256())
    import base64 as _b64
    sig_b64 = _b64.b64encode(sig_bytes).decode()

    r = _client().post(
        "/u/alice/inbox",
        content=body,
        headers={
            "Content-Type": "application/activity+json",
            "Date": date,
            "Digest": digest,
            "Host": "testserver",
            "Signature": (
                f'keyId="{remote_actor_url}#main-key",'
                f'algorithm="rsa-sha256",'
                f'headers="(request-target) host date digest",'
                f'signature="{sig_b64}"'
            ),
        },
    )
    assert r.status_code == 202

    # RemoteFollow row landed.
    with Session(db_mod.engine) as s:
        rf = s.exec(select(db_mod.RemoteFollow)).one()
        assert rf.follow_activity_id == follow["id"]

    # We attempted an Accept delivery to their inbox.
    assert len(sent) == 1
    assert sent[0]["url"] == "https://mastodon.example/users/foo/inbox"
    accept = json.loads(sent[0]["body"])
    assert accept["type"] == "Accept"
    assert accept["object"]["type"] == "Follow"


def test_followers_collection_lists_accepted_follows():
    """The /followers endpoint returns an OrderedCollection of
    remote actor URLs so Mastodon can show the follower count."""
    import app.db as db_mod

    c = _client()
    _login(c, "alice@example.com")

    # Insert a fake remote follower directly (bypassing the inbox
    # crypto dance — we cover that above).
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        ra = db_mod.RemoteActor(
            actor_url="https://mastodon.example/users/bar",
            inbox_url="https://mastodon.example/users/bar/inbox",
            public_key_pem="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----",
        )
        s.add(ra)
        s.commit()
        s.refresh(ra)
        s.add(db_mod.RemoteFollow(
            remote_actor_id=ra.id,
            user_id=alice.id,
            follow_activity_id="https://mastodon.example/users/bar/follows/1",
        ))
        s.commit()

    # /u/alice/followers is shared between the HTML followers page
    # (default) and the AP OrderedCollection (Accept: activity+json).
    # Mastodon uses the latter.
    r = _client().get(
        "/u/alice/followers",
        headers={"Accept": "application/activity+json"},
    )
    assert r.status_code == 200
    assert "activity+json" in r.headers["content-type"]
    body = r.json()
    assert body["totalItems"] == 1
    assert "https://mastodon.example/users/bar" in body["orderedItems"]


# ─── M10: outbound Follow + inbound Note ingestion (0.11.30) ──────────


def test_remote_follow_page_lists_pending_followings(monkeypatch):
    """POST /follow/remote resolves WebFinger, records a pending
    RemoteFollowing, and the page then lists it as ⏳ pending."""
    import app.activitypub as ap
    import app.db as db_mod

    c = _client()
    _login(c, "alice@example.com")

    # Stub WebFinger + actor fetch + Follow POST so we don't hit the network.
    monkeypatch.setattr(
        ap, "resolve_remote_handle",
        lambda h: "https://mastodon.example/users/foo",
    )
    monkeypatch.setattr(
        ap, "_fetch_remote_actor_doc",
        lambda url, as_user=None: {
            "id": "https://mastodon.example/users/foo",
            "inbox": "https://mastodon.example/users/foo/inbox",
            "preferredUsername": "foo",
            "name": "Foo",
            "publicKey": {
                "id": "https://mastodon.example/users/foo#main-key",
                "owner": "https://mastodon.example/users/foo",
                "publicKeyPem": "-----BEGIN PUBLIC KEY-----\nAAA\n-----END PUBLIC KEY-----",
            },
        },
    )

    class _R:
        status_code = 202
        text = ""

    monkeypatch.setattr(
        ap, "_sign_and_post",
        lambda priv, kid, url, body: _R(),
    )

    r = c.post("/follow/remote", data={"handle": "@foo@mastodon.example"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/follow/remote?ok=1"

    # RemoteFollowing row exists, pending.
    with Session(db_mod.engine) as s:
        rows = s.exec(select(db_mod.RemoteFollowing)).all()
        assert len(rows) == 1
        assert rows[0].accepted_at is None

    # Page renders the pending row.
    r = c.get("/follow/remote")
    assert "@foo@mastodon.example" in r.text
    assert "pending" in r.text


def test_remote_follow_bad_handle_returns_400():
    c = _client()
    _login(c, "alice@example.com")
    r = c.post("/follow/remote", data={"handle": "not-a-handle"},
               follow_redirects=False)
    # 4xx cascades through to a flash on the page (via ?err=…), not a
    # raw error page.
    assert r.status_code == 303
    assert "err=400" in r.headers["location"]


# ─── NodeInfo (0.11.31) ────────────────────────────────────────────────


def test_nodeinfo_discovery_points_at_v2():
    r = _client().get("/.well-known/nodeinfo")
    assert r.status_code == 200
    body = r.json()
    assert body["links"][0]["rel"] == "http://nodeinfo.diaspora.software/ns/schema/2.0"
    assert body["links"][0]["href"] == "https://rssfriends.com/nodeinfo/2.0"


def test_nodeinfo_v2_payload_shape():
    r = _client().get("/nodeinfo/2.0")
    assert r.status_code == 200
    # Content-Type must include the NodeInfo profile parameter.
    ct = r.headers["content-type"]
    assert "application/json" in ct
    assert "nodeinfo.diaspora.software/ns/schema/2.0" in ct

    body = r.json()
    assert body["version"] == "2.0"
    assert body["software"]["name"] == "rssfriends"
    assert body["protocols"] == ["activitypub"]
    assert body["openRegistrations"] is True
    # Structure the fediverse crawler cares about.
    for k in ("total", "activeMonth", "activeHalfyear"):
        assert k in body["usage"]["users"]
    assert "localPosts" in body["usage"]
    assert body["metadata"]["nodeName"] == "rssFriends"


# ─── 0.11.33: /feed must not 500 when a remote_note is present ────────


def test_feed_renders_with_remote_note_entries():
    """Regression: after 0.11.30 shipped M10, /feed raised
    KeyError: 'activity' in the timeline enrichment loops when the
    entry list contained remote_note dicts (which don't carry a
    local Activity row). Fixed in 0.11.33 by skipping remote_note
    entries in the enrichment. Test seeds one accepted-follow + one
    RemoteItem and asserts /feed renders 200 with the note visible."""
    from datetime import UTC, datetime

    import app.db as db_mod

    c = _client()
    _login(c, "alice@example.com")

    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User)
                       .where(db_mod.User.handle == "alice")).one()
        ra = db_mod.RemoteActor(
            actor_url="https://mastodon.example/users/foo",
            inbox_url="https://mastodon.example/users/foo/inbox",
            public_key_pem="-----BEGIN PUBLIC KEY-----\nXX\n-----END PUBLIC KEY-----",
            preferred_username="foo",
            display_name="Foo",
        )
        s.add(ra)
        s.commit()
        s.refresh(ra)
        s.add(db_mod.RemoteFollowing(
            user_id=alice.id,
            remote_actor_id=ra.id,
            follow_activity_id="https://rssfriends.com/u/alice#follows/1",
            accepted_at=datetime.now(UTC),
        ))
        s.add(db_mod.RemoteItem(
            remote_actor_id=ra.id,
            ap_note_id="https://mastodon.example/notes/1",
            content_html="<p>Hello from Mastodon</p>",
            note_url="https://mastodon.example/@foo/1",
            published_at=datetime.now(UTC),
        ))
        s.commit()

    r = c.get("/feed")
    assert r.status_code == 200, r.text[:400]
    assert "Hello from Mastodon" in r.text
    # Rendered as a remote-note row, not raw activity markup.
    assert "🐘" in r.text
