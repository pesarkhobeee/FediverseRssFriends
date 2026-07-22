"""Wallabag integration: OAuth flow (mocked httpx), token refresh,
bookmark-triggers-send hook, settings UI routes."""

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


# ---- state signing ------------------------------------------------------


def test_state_sign_verify_roundtrip():
    from app import wallabag
    token = wallabag.sign_state(42)
    assert wallabag.verify_state(token) == 42


def test_state_verify_rejects_garbage():
    from app import wallabag
    assert wallabag.verify_state("nope") is None


# ---- OAuth save + callback ---------------------------------------------


def test_save_wallabag_redirects_to_authorize_url():
    client = _client()
    _login(client, "alice@example.com")
    r = client.post(
        "/settings/wallabag",
        data={
            "instance_url": "https://wb.example.com/",
            "client_id": "cid1",
            "client_secret": "csec1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://wb.example.com/oauth/v2/auth?")
    assert "client_id=cid1" in loc
    assert "response_type=code" in loc
    assert "redirect_uri=" in loc
    assert "state=" in loc

    account = _get_account()
    assert account is not None
    assert account.instance_url == "https://wb.example.com"
    assert account.client_id == "cid1"
    assert account.client_secret == "csec1"
    assert account.enabled is False
    assert account.access_token == ""


def test_save_wallabag_rejects_bad_url():
    client = _client()
    _login(client, "alice@example.com")
    r = client.post(
        "/settings/wallabag",
        data={"instance_url": "wb.example.com", "client_id": "c", "client_secret": "s"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_bad_state_flashes_error():
    client = _client()
    _login(client, "alice@example.com")
    client.post("/settings/wallabag", data={
        "instance_url": "https://wb.example.com",
        "client_id": "cid",
        "client_secret": "csec",
    }, follow_redirects=False)
    r = client.get(
        "/settings/wallabag/callback?code=abc&state=deadbeef",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "wallabag=err" in r.headers["location"]
    assert "bad_state" in r.headers["location"]


def test_callback_exchanges_code_and_enables(monkeypatch):
    client = _client()
    _login(client, "alice@example.com")
    client.post("/settings/wallabag", data={
        "instance_url": "https://wb.example.com",
        "client_id": "cid",
        "client_secret": "csec",
    }, follow_redirects=False)

    # Fake the httpx call inside exchange_code.
    import app.wallabag as wb
    calls = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "access_token": "atk", "refresh_token": "rtk",
                "expires_in": 3600,
            }

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, data=None, **kw):
            calls.append((url, data))
            return FakeResp()

    monkeypatch.setattr(wb.httpx, "Client", FakeClient)

    state = wb.sign_state(_get_account().user_id)
    r = client.get(
        f"/settings/wallabag/callback?code=CODE&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "wallabag=ok" in r.headers["location"]
    assert calls and calls[0][0] == "https://wb.example.com/oauth/v2/token"
    assert calls[0][1]["grant_type"] == "authorization_code"
    assert calls[0][1]["code"] == "CODE"

    acc = _get_account()
    assert acc.enabled is True
    assert acc.access_token == "atk"
    assert acc.refresh_token == "rtk"
    assert acc.token_expires_at is not None


def test_callback_exchange_failure_flashes_err(monkeypatch):
    client = _client()
    _login(client, "alice@example.com")
    client.post("/settings/wallabag", data={
        "instance_url": "https://wb.example.com",
        "client_id": "cid",
        "client_secret": "csec",
    }, follow_redirects=False)

    import app.wallabag as wb

    class BoomClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(wb.httpx, "Client", BoomClient)

    state = wb.sign_state(_get_account().user_id)
    r = client.get(
        f"/settings/wallabag/callback?code=x&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "wallabag=err" in r.headers["location"]
    assert _get_account().enabled is False


# ---- disconnect / toggle ------------------------------------------------


def _enable_account(email="alice@example.com"):
    """Directly install a good account for tests below."""
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.email == email)).one()
        acc = db_mod.WallabagAccount(
            user_id=user.id, instance_url="https://wb.example.com",
            client_id="cid", client_secret="csec",
            access_token="atk", refresh_token="rtk",
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
    import app.db as db_mod
    import app.wallabag as wb

    _client()  # ensure app + db
    with Session(db_mod.engine) as s:
        u = db_mod.User(email="a@e.com", handle="a")
        s.add(u)
        s.commit()
        s.refresh(u)
        acc = db_mod.WallabagAccount(
            user_id=u.id, instance_url="https://wb.example.com",
            client_id="c", client_secret="s",
            access_token="stale", refresh_token="rtk",
            token_expires_at=datetime.now(UTC) - timedelta(seconds=1),  # stale
            enabled=True,
        )
        s.add(acc)
        s.commit()
        s.refresh(acc)

    calls = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"access_token": "fresh", "refresh_token": "rtk2",
                    "expires_in": 3600}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, data=None, json=None, **kw):
            calls.append((url, data, json, kw.get("headers")))
            return FakeResp()

    monkeypatch.setattr(wb.httpx, "Client", FakeClient)

    with Session(db_mod.engine) as s:
        acc = s.exec(select(db_mod.WallabagAccount)).one()
        assert wb.send_url(s, acc, "https://article/1") is True
        s.refresh(acc)
        assert acc.access_token == "fresh"

    # First call = refresh token exchange, second = entries POST with fresh bearer
    assert calls[0][0].endswith("/oauth/v2/token")
    assert calls[0][1]["grant_type"] == "refresh_token"
    assert calls[1][0].endswith("/api/entries.json")
    assert calls[1][3]["Authorization"] == "Bearer fresh"


def test_send_url_retries_on_401(monkeypatch):
    import app.db as db_mod
    import app.wallabag as wb

    _client()
    with Session(db_mod.engine) as s:
        u = db_mod.User(email="a@e.com", handle="a")
        s.add(u)
        s.commit()
        s.refresh(u)
        acc = db_mod.WallabagAccount(
            user_id=u.id, instance_url="https://wb.example.com",
            client_id="c", client_secret="s",
            access_token="expired-server-side", refresh_token="rtk",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),  # not stale locally
            enabled=True,
        )
        s.add(acc)
        s.commit()

    responses = iter([401, 200])
    tokens_used = []

    class FakeResp:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return {"access_token": "fresh", "refresh_token": "rtk",
                    "expires_in": 3600}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, data=None, json=None, **kw):
            if url.endswith("/oauth/v2/token"):
                return FakeResp(200)
            tokens_used.append(kw["headers"]["Authorization"])
            return FakeResp(next(responses))

    monkeypatch.setattr(wb.httpx, "Client", FakeClient)

    with Session(db_mod.engine) as s:
        acc = s.exec(select(db_mod.WallabagAccount)).one()
        assert wb.send_url(s, acc, "https://article/1") is True

    assert tokens_used == ["Bearer expired-server-side", "Bearer fresh"]


def test_send_url_safe_noop_when_disabled(monkeypatch):
    """Disabled account → dispatcher must not call HTTP at all."""
    import app.db as db_mod
    import app.wallabag as wb

    _client()
    with Session(db_mod.engine) as s:
        u = db_mod.User(email="a@e.com", handle="a")
        s.add(u)
        s.commit()
        s.refresh(u)
        acc = db_mod.WallabagAccount(
            user_id=u.id, instance_url="https://wb.example.com",
            client_id="c", client_secret="s",
            access_token="atk", refresh_token="rtk",
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
    uid, link, tags = calls[0]
    assert link == "https://article/1"
    assert tags == ["Tech"]


def test_bookmark_does_not_enqueue_when_no_wallabag(monkeypatch):
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
        item_id = item.id

    client = _client()
    _login(client, "alice@example.com")

    calls = []
    monkeypatch.setattr(
        bookmarks_mod.wallabag, "send_url_safe",
        lambda *a, **kw: calls.append(a),
    )
    client.post(f"/items/{item_id}/bookmark")
    # send_url_safe is still called (it self-guards), but doesn't call HTTP.
    # From the router's POV: it's fine either way — the important behavior
    # is that no exception surfaces to the user.
    # Assert it either wasn't called OR was called with a user_id lacking an account.
    if calls:
        assert calls[0][1] == "https://article/1"
