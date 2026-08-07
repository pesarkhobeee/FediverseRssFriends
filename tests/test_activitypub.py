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


def test_inbox_returns_501_not_implemented():
    """Discovery-only: inbound Follow / Create posts get 501 so remote
    servers stop retrying instead of thinking we've temporarily failed."""
    c = _client()
    _login(c, "alice@example.com")
    r = _client().post(
        "/u/alice/inbox",
        content=json.dumps({"type": "Follow", "actor": "https://x.example/actor"}),
        headers={"Content-Type": "application/activity+json"},
    )
    assert r.status_code == 501
