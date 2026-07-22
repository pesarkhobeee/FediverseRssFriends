"""Wallabag integration: password-grant token exchange (mocked httpx),
token refresh, bookmark-triggers-send hook, settings UI routes.

Wallabag's public OAuth API only supports grant_type=password; there
is no authorization-code endpoint. See app/wallabag.py for details."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select


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


def _get_account(user_email="alice@example.com"):
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        user = s.exec(
            select(db_mod.User).where(db_mod.User.email == user_email)
        ).one()
        return s.exec(
            select(db_mod.WallabagAccount).where(
                db_mod.WallabagAccount.user_id == user.id
            )
        ).first()


def _install_fake_httpx(monkeypatch, responses):
    """Install a FakeClient on app.wallabag.httpx.Client that returns
    the next response from `responses` (a list of dicts describing the
    reply) and records each call in the returned list."""
    import app.wallabag as wb

    calls = []
    resp_iter = iter(responses)

    class FakeResp:
        def __init__(self, spec):
            self.status_code = spec.get("status", 200)
            self._json = spec.get("json", {})
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return self._json

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, data=None, json=None, **kw):
            calls.append({
                "url": url, "data": data, "json": json,
                "headers": kw.get("headers"),
            })
            return FakeResp(next(resp_iter))

    monkeypatch.setattr(wb.httpx, "Client", FakeClient)
    return calls


# ---- POST /settings/wallabag ------------------------------------------


def test_save_wallabag_password_grant_success(monkeypatch):
    calls = _install_fake_httpx(monkeypatch, [
        {"status": 200, "json": {
            "access_token": "atk", "refresh_token": "rtk", "expires_in": 3600,
        }},
    ])
    client = _client()
    _login(client, "alice@example.com")

    r = client.post(
        "/settings/wallabag",
        data={
            "instance_url": "https://wb.example.com/",
            "client_id": "cid1",
            "client_secret": "csec1",
            "username": "alice",
            "password": "hunter2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "wallabag=ok" in r.headers["location"]

    # Exchange hit /oauth/v2/token with grant_type=password.
    assert calls[0]["url"] == "https://wb.example.com/oauth/v2/token"
    d = calls[0]["data"]
    assert d["grant_type"] == "password"
    assert d["client_id"] == "cid1"
    assert d["client_secret"] == "csec1"
    assert d["username"] == "alice"
    assert d["password"] == "hunter2"

    from app.crypto import decrypt
    acc = _get_account()
    assert acc is not None
    assert acc.enabled is True
    # Tokens + password + client_secret are stored encrypted on disk;
    # only decrypt() recovers the plaintext.
    assert acc.access_token != "atk"
    assert decrypt(acc.access_token) == "atk"
    assert decrypt(acc.refresh_token) == "rtk"
    assert decrypt(acc.client_secret) == "csec1"
    assert decrypt(acc.password) == "hunter2"
    assert acc.username == "alice"  # not a secret; kept plaintext
    assert acc.token_expires_at is not None


def test_encrypted_fields_are_opaque_at_rest():
    """A sanity check that a plaintext scan of the row cannot recover
    the credential — mimics the 'database was stolen' scenario."""
    from app.crypto import encrypt
    ct = encrypt("hunter2")
    assert ct != "hunter2"
    assert "hunter2" not in ct  # base64 output doesn't contain plaintext substr


def test_save_wallabag_password_grant_failure(monkeypatch):
    _install_fake_httpx(monkeypatch, [
        {"status": 400, "json": {"error": "invalid_grant"}},
    ])
    client = _client()
    _login(client, "alice@example.com")

    r = client.post(
        "/settings/wallabag",
        data={
            "instance_url": "https://wb.example.com",
            "client_id": "cid", "client_secret": "csec",
            "username": "alice", "password": "wrong",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "wallabag=err" in r.headers["location"]
    # Row is saved so the user can fix and retry, but not enabled.
    acc = _get_account()
    assert acc is not None
    assert acc.enabled is False
    assert acc.access_token == ""


def test_save_wallabag_rejects_bad_url():
    client = _client()
    _login(client, "alice@example.com")
    r = client.post(
        "/settings/wallabag",
        data={
            "instance_url": "wb.example.com",
            "client_id": "c", "client_secret": "s",
            "username": "u", "password": "p",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


# ---- disconnect / toggle ------------------------------------------------


def _enable_account(email="alice@example.com"):
    """Directly install a good account for tests below."""
    import app.db as db_mod
    from app.crypto import encrypt
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.email == email)).one()
        acc = db_mod.WallabagAccount(
            user_id=user.id, instance_url="https://wb.example.com",
            client_id="cid", client_secret=encrypt("csec"),
            username="alice", password=encrypt("hunter2"),
            access_token=encrypt("atk"), refresh_token=encrypt("rtk"),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            enabled=True,
        )
        s.add(acc)
        s.commit()


def test_disconnect_removes_row():
    client = _client()
    _login(client, "alice@example.com")
    _enable_account()
    assert _get_account() is not None
    r = client.post("/settings/wallabag/disconnect", follow_redirects=False)
    assert r.status_code == 303
    assert _get_account() is None


def test_toggle_pauses_and_resumes():
    client = _client()
    _login(client, "alice@example.com")
    _enable_account()

    r = client.post("/settings/wallabag/toggle", data={"enabled": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _get_account().enabled is False

    r = client.post("/settings/wallabag/toggle", data={"enabled": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _get_account().enabled is True


# ---- send_url / refresh flow -------------------------------------------


def test_send_url_refreshes_stale_token(monkeypatch):
    """A stale access_token triggers a refresh_token grant before the
    entries.json POST goes out with the new bearer."""
    import app.db as db_mod
    from app.crypto import encrypt
    _client()
    with Session(db_mod.engine) as s:
        u = db_mod.User(email="a@e.com", handle="a")
        s.add(u)
        s.commit()
        s.refresh(u)
        acc = db_mod.WallabagAccount(
            user_id=u.id, instance_url="https://wb.example.com",
            client_id="c", client_secret="s",
            username="a", password=encrypt("p"),
            access_token=encrypt("stale"), refresh_token=encrypt("rtk"),
            token_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            enabled=True,
        )
        s.add(acc)
        s.commit()

    calls = _install_fake_httpx(monkeypatch, [
        {"status": 200, "json": {  # refresh_token exchange
            "access_token": "fresh", "refresh_token": "rtk2",
            "expires_in": 3600,
        }},
        {"status": 200},  # entries.json POST
    ])

    import app.wallabag as wb
    with Session(db_mod.engine) as s:
        acc = s.exec(select(db_mod.WallabagAccount)).one()
        assert wb.send_url(s, acc, "https://article/1") is True
        s.refresh(acc)
        from app.crypto import decrypt as _dec
        assert _dec(acc.access_token) == "fresh"

    assert calls[0]["url"].endswith("/oauth/v2/token")
    assert calls[0]["data"]["grant_type"] == "refresh_token"
    assert calls[1]["url"].endswith("/api/entries.json")
    assert calls[1]["headers"]["Authorization"] == "Bearer fresh"


def test_send_url_retries_on_401(monkeypatch):
    """Locally-fresh token that the server rejects with 401 → refresh
    then retry the POST."""
    import app.db as db_mod
    from app.crypto import encrypt
    _client()
    with Session(db_mod.engine) as s:
        u = db_mod.User(email="a@e.com", handle="a")
        s.add(u)
        s.commit()
        s.refresh(u)
        acc = db_mod.WallabagAccount(
            user_id=u.id, instance_url="https://wb.example.com",
            client_id="c", client_secret="s",
            username="a", password=encrypt("p"),
            access_token=encrypt("expired-server-side"), refresh_token=encrypt("rtk"),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            enabled=True,
        )
        s.add(acc)
        s.commit()

    calls = _install_fake_httpx(monkeypatch, [
        {"status": 401},  # first entries.json POST
        {"status": 200, "json": {  # refresh_token exchange
            "access_token": "fresh", "refresh_token": "rtk",
            "expires_in": 3600,
        }},
        {"status": 200},  # retry entries.json POST
    ])

    import app.wallabag as wb
    with Session(db_mod.engine) as s:
        acc = s.exec(select(db_mod.WallabagAccount)).one()
        assert wb.send_url(s, acc, "https://article/1") is True

    posts = [c for c in calls if c["url"].endswith("/api/entries.json")]
    assert [p["headers"]["Authorization"] for p in posts] == [
        "Bearer expired-server-side", "Bearer fresh",
    ]


def test_refresh_falls_back_to_password_grant(monkeypatch):
    """If refresh_token is rejected, the client falls back to a fresh
    password_grant with the stored credentials."""
    import app.db as db_mod
    from app.crypto import encrypt
    _client()
    with Session(db_mod.engine) as s:
        u = db_mod.User(email="a@e.com", handle="a")
        s.add(u)
        s.commit()
        s.refresh(u)
        acc = db_mod.WallabagAccount(
            user_id=u.id, instance_url="https://wb.example.com",
            client_id="c", client_secret="s",
            username="a", password=encrypt("p"),
            access_token=encrypt("stale"), refresh_token=encrypt("rotten"),
            token_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            enabled=True,
        )
        s.add(acc)
        s.commit()

    calls = _install_fake_httpx(monkeypatch, [
        {"status": 400, "json": {"error": "invalid_grant"}},  # refresh_token fails
        {"status": 200, "json": {  # password grant fallback
            "access_token": "fresh", "refresh_token": "rtk2",
            "expires_in": 3600,
        }},
        {"status": 200},  # entries.json POST
    ])

    import app.wallabag as wb
    with Session(db_mod.engine) as s:
        acc = s.exec(select(db_mod.WallabagAccount)).one()
        assert wb.send_url(s, acc, "https://article/1") is True

    grants = [c["data"]["grant_type"] for c in calls if c["url"].endswith("/oauth/v2/token")]
    assert grants == ["refresh_token", "password"]


def test_send_url_safe_noop_when_disabled(monkeypatch):
    """Disabled account → dispatcher must not call HTTP at all."""
    import app.db as db_mod
    import app.wallabag as wb
    from app.crypto import encrypt
    _client()
    with Session(db_mod.engine) as s:
        u = db_mod.User(email="a@e.com", handle="a")
        s.add(u)
        s.commit()
        s.refresh(u)
        acc = db_mod.WallabagAccount(
            user_id=u.id, instance_url="https://wb.example.com",
            client_id="c", client_secret="s",
            username="a", password=encrypt("p"),
            access_token=encrypt("atk"), refresh_token=encrypt("rtk"),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            enabled=False,
        )
        s.add(acc)
        s.commit()
        uid = u.id

    def boom(*a, **kw):
        raise AssertionError("HTTP client should not be constructed")

    monkeypatch.setattr(wb.httpx, "Client", boom)
    wb.send_url_safe(uid, "https://article/1")  # must not raise


def test_send_url_safe_noop_when_no_account(monkeypatch):
    """No WallabagAccount row → dispatcher just returns."""
    import app.wallabag as wb

    _client()

    def boom(*a, **kw):
        raise AssertionError("HTTP client should not be constructed")

    monkeypatch.setattr(wb.httpx, "Client", boom)
    wb.send_url_safe(999, "https://article/1")


# ---- bookmark hook ------------------------------------------------------


def test_bookmark_enqueues_wallabag_send_when_enabled(monkeypatch):
    """A POST /items/{id}/bookmark with a linked-and-enabled Wallabag
    account fires the send_url_safe background task exactly once with
    the item link + the user's category name as a tag."""
    import app.bookmarks as bookmarks_mod
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Ex")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="g", title="Post",
            link="https://article/1",
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        feed_id, item_id = feed.id, item.id

    client = _client()
    _login(client, "alice@example.com")
    _enable_account()

    # Give alice a category + subscription so the hook picks up a tag.
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        cat = db_mod.Category(user_id=alice.id, name="Tech", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        s.add(db_mod.Subscription(
            user_id=alice.id, feed_id=feed_id, category_id=cat.id, sort_order=1,
        ))
        s.commit()

    calls = []

    def fake_send(user_id, link, tags=None):
        calls.append((user_id, link, tags))

    monkeypatch.setattr(bookmarks_mod.wallabag, "send_url_safe", fake_send)

    r = client.post(f"/items/{item_id}/bookmark")
    assert r.status_code == 200
    assert len(calls) == 1
    _uid, link, tags = calls[0]
    assert link == "https://article/1"
    assert tags == ["Tech"]


def test_bookmark_does_not_crash_when_no_wallabag(monkeypatch):
    """User without a WallabagAccount can still bookmark — the background
    dispatcher self-guards and doesn't call HTTP."""
    import app.db as db_mod
    import app.wallabag as wb

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Ex")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="g", title="Post",
            link="https://article/1",
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        item_id = item.id

    def boom(*a, **kw):
        raise AssertionError("no HTTP call expected")

    monkeypatch.setattr(wb.httpx, "Client", boom)

    client = _client()
    _login(client, "alice@example.com")
    r = client.post(f"/items/{item_id}/bookmark")
    assert r.status_code == 200
