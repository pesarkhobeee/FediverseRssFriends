"""Tests for the background refresh: fetch_one, cycle, dedup, error path."""

from unittest.mock import MagicMock

from sqlmodel import Session, select

SAMPLE_ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Feed</title>
  <link href="https://example.com/"/>
  <updated>2026-06-01T12:00:00Z</updated>

  <entry>
    <title>First post</title>
    <link href="https://example.com/1"/>
    <id>urn:example:1</id>
    <updated>2026-06-01T12:00:00Z</updated>
    <summary>Hello world.</summary>
  </entry>
  <entry>
    <title>Second post</title>
    <link href="https://example.com/2"/>
    <id>urn:example:2</id>
    <updated>2026-06-02T09:00:00Z</updated>
    <summary>Another one.</summary>
  </entry>
</feed>
"""


def _stub_httpx(monkeypatch, body: bytes = SAMPLE_ATOM, status_code: int = 200):
    import app.refresh as ref

    class FakeResp:
        content = body

        def raise_for_status(self):
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp()

    monkeypatch.setattr(ref.httpx, "Client", FakeClient)


def _seed_feed(url: str = "https://example.com/feed.xml"):
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url=url, title="stub")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        return feed.id


def test_fetch_one_creates_items(monkeypatch):
    _stub_httpx(monkeypatch)
    feed_id = _seed_feed()

    import app.db as db_mod
    from app.refresh import fetch_one

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        new, err = fetch_one(s, feed)
        assert err is None
        assert new == 2
        assert feed.title == "Example Feed"
        assert feed.last_error == ""
        assert feed.consecutive_failures == 0
        items = s.exec(select(db_mod.FeedItem).where(db_mod.FeedItem.feed_id == feed_id)).all()
        assert {i.title for i in items} == {"First post", "Second post"}


def test_fetch_one_is_idempotent(monkeypatch):
    _stub_httpx(monkeypatch)
    feed_id = _seed_feed()

    import app.db as db_mod
    from app.refresh import fetch_one

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        fetch_one(s, feed)
        new_again, _ = fetch_one(s, feed)
        assert new_again == 0
        items = s.exec(select(db_mod.FeedItem).where(db_mod.FeedItem.feed_id == feed_id)).all()
        assert len(items) == 2


def test_fetch_one_error_path_records_failure(monkeypatch):
    import app.refresh as ref

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(ref.httpx, "Client", MagicMock(side_effect=_boom))
    feed_id = _seed_feed()

    import app.db as db_mod
    from app.refresh import fetch_one

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        new, err = fetch_one(s, feed)
        assert new == 0
        assert err and "network down" in err
        assert feed.consecutive_failures == 1
        assert "network down" in feed.last_error


def test_run_cycle_processes_due_feeds(monkeypatch):
    _stub_httpx(monkeypatch)
    _seed_feed("https://a.example/feed")
    _seed_feed("https://b.example/feed")

    from app.refresh import run_cycle

    result = run_cycle()
    assert result["feeds"] == 2
    assert result["new_items"] == 4
    assert result["errors"] == 0


def test_manual_refresh_endpoint_requires_login():
    from fastapi.testclient import TestClient

    import app.main

    r = TestClient(app.main.app).post("/refresh")
    assert r.status_code == 401


def test_manual_refresh_endpoint_ok(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main

    _stub_httpx(monkeypatch)
    _seed_feed()

    client = TestClient(app.main.app)
    # Log in
    r = client.post("/login", data={"email": "z@example.com"})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    token = r.text[idx:end].split("token=", 1)[1]

    client.post("/auth/verify", data={"token": token}, follow_redirects=False)

    r2 = client.post("/refresh")
    assert r2.status_code == 200
    assert "added 2 items" in r2.text
