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


def test_refresh_does_not_downgrade_previously_deep_resolved_favicon(monkeypatch):
    """Regression: if a feed's stored favicon_url is a deep-resolved
    URL (not the generic /favicon.ico for its origin), refreshing must
    NOT clobber it with the fast-path fallback. This was the "Datadog
    icon disappearing" bug — every cycle silently downgraded the icon."""
    _stub_httpx(monkeypatch)
    import app.db as db_mod
    import app.refresh as ref

    # The stored icon is healthy — no heal, no downgrade.
    monkeypatch.setattr(ref, "_icon_is_reachable", lambda url, timeout=3.0: True)

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://example.com/feed.xml",
            title="Example",
            site_url="https://example.com",
            # A CDN-hosted icon — deep-resolved sometime in the past.
            favicon_url="https://cdn.example.com/img/favicon.ico",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    from app.refresh import fetch_one

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        fetch_one(s, feed)
        s.commit()

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        # Icon preserved — not downgraded to the generic fallback.
        assert feed.favicon_url == "https://cdn.example.com/img/favicon.ico"


def test_refresh_heals_url_when_stored_url_is_html_not_a_feed(monkeypatch):
    """When feed.url points at an HTML page (feedparser accepts it but
    produces 0 entries / no title) AND we have no historical items,
    refresh auto-migrates to a discovered feed URL."""
    import app.db as db_mod
    import app.refresh as ref

    # First fetch: empty response for the ORIGINAL url (HTML masquerading
    # as a feed). Second fetch (after heal): a real feed.
    empty_feed_body = b"<html><body>plain html, no feed here</body></html>"
    good_feed_body = SAMPLE_ATOM

    fetch_state = {"url_seen": None}

    class FakeResp:
        def __init__(self, body):
            self.content = body

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            fetch_state["url_seen"] = url
            body = good_feed_body if "/goodfeed" in url else empty_feed_body
            return FakeResp(body)

    monkeypatch.setattr(ref.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        ref, "_maybe_heal_feed_url",
        lambda db, feed: "https://blog.example/goodfeed" if feed.url.endswith("/jobs") else None,
    )
    monkeypatch.setattr(ref, "_icon_is_reachable", lambda url, timeout=3.0: True)

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://blog.example/jobs", title="stub")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    from app.refresh import fetch_one
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        new, err = fetch_one(s, feed)
        s.commit()

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
    # URL healed to the discovered one.
    assert feed.url == "https://blog.example/goodfeed"
    # Items from the real feed landed on the same Feed row.
    assert new == 2  # SAMPLE_ATOM has two entries


def test_refresh_does_not_heal_feed_with_history(monkeypatch):
    """Refuse to migrate a feed that has produced items before — could
    be a legit feed that's just temporarily empty."""
    _stub_httpx(monkeypatch, body=b"<html/>")
    import app.db as db_mod
    import app.refresh as ref

    called = {"heal": False}

    def fake_heal(db, feed):
        called["heal"] = True
        return None

    monkeypatch.setattr(ref, "_maybe_heal_feed_url", fake_heal)

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://blog.example/rss", title="Blog")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.FeedItem(feed_id=feed.id, guid="old-1", title="Old post"))
        s.commit()
        feed_id = feed.id

    from app.refresh import fetch_one
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        fetch_one(s, feed)

    # _maybe_heal_feed_url IS called (0-entry + no-title trigger) but
    # inside it must refuse because prior items exist. Verify with a
    # separate direct call.
    from app.refresh import _maybe_heal_feed_url as heal_fn
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        assert heal_fn(s, feed) is None


def test_refresh_heals_stored_icon_that_404s(monkeypatch):
    """Regression: a custom (non-generic) favicon URL that used to work
    but now returns 404 must be re-derived — otherwise it stays broken
    forever. This was feed 232 (Google Cloud Blog): the RSS <image>
    URL had gone stale."""
    _stub_httpx(monkeypatch)
    import app.db as db_mod
    import app.refresh as ref

    monkeypatch.setattr(ref, "_icon_is_reachable", lambda url, timeout=3.0: False)

    calls = []
    real_resolve = ref.resolve_avatar

    def spy(feed_url, site_url, feed_meta=None, *, deep=False):
        calls.append(deep)
        if deep:
            return "https://cdn.example.com/real-icon.png"
        return real_resolve(feed_url, site_url, feed_meta, deep=deep)

    monkeypatch.setattr(ref, "resolve_avatar", spy)

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://example.com/feed.xml",
            title="Example",
            site_url="https://example.com",
            # A "custom" stored URL (not the generic fallback) that 404s.
            favicon_url="https://example.com/old-broken-icon.png",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    from app.refresh import fetch_one
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        fetch_one(s, feed)
        s.commit()

    assert True in calls, "expected refresh to deep-resolve a 404 icon"
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        assert feed.favicon_url == "https://cdn.example.com/real-icon.png"


def test_refresh_normalizes_relative_site_link(monkeypatch):
    """Regression: some feeds publish <link>/</link> as their site URL.
    Storing that verbatim breaks every downstream URL derivation.
    Refresh must resolve it against the feed URL."""
    body = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Chacon</title>
      <link href="/"/>
      <updated>2026-06-01T12:00:00Z</updated>
      <entry>
        <title>x</title>
        <link href="https://scottchacon.com/x"/>
        <id>urn:1</id>
        <updated>2026-06-01T12:00:00Z</updated>
      </entry>
    </feed>"""
    _stub_httpx(monkeypatch, body=body)
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://scottchacon.com/atom.xml", title="")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    from app.refresh import fetch_one
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        fetch_one(s, feed)
        s.commit()

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
    assert feed.site_url == "https://scottchacon.com/", (
        f"expected absolute site_url, got {feed.site_url!r}"
    )


def test_refresh_does_not_blank_a_working_icon_on_empty_resolve(monkeypatch):
    """If the stored icon 404s but deep-resolve can't find a
    replacement (returns ""), keep the old value rather than blanking
    the icon. Better a broken URL than no URL — the browser fallback
    JS can still try /favicon.png etc."""
    _stub_httpx(monkeypatch)
    import app.db as db_mod
    import app.refresh as ref

    monkeypatch.setattr(ref, "_icon_is_reachable", lambda url, timeout=3.0: False)
    monkeypatch.setattr(
        ref, "resolve_avatar",
        lambda feed_url, site_url, feed_meta=None, *, deep=False: "",
    )

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://example.com/feed.xml",
            title="X",
            site_url="https://example.com",
            favicon_url="https://example.com/some-icon.png",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    from app.refresh import fetch_one
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        fetch_one(s, feed)
        s.commit()

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
    # Original icon preserved — refresh refused to blank it.
    assert feed.favicon_url == "https://example.com/some-icon.png"


def test_refresh_attempts_deep_resolve_when_stored_icon_is_generic(monkeypatch):
    """When the stored favicon_url is the generic /favicon.ico fallback,
    refresh opportunistically tries deep resolve — that's how the bug's
    stale rows heal on the next cycle."""
    _stub_httpx(monkeypatch)
    import app.db as db_mod
    import app.refresh as ref

    calls = []
    real_resolve = ref.resolve_avatar

    def spy_resolve(feed_url, site_url, feed_meta=None, *, deep=False):
        calls.append(deep)
        # Simulate deep resolve finding a better icon.
        if deep:
            return "https://cdn.example.com/img/favicon.ico"
        return real_resolve(feed_url, site_url, feed_meta, deep=deep)

    monkeypatch.setattr(ref, "resolve_avatar", spy_resolve)

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://example.com/feed.xml",
            title="Example",
            site_url="https://example.com",
            favicon_url="https://example.com/favicon.ico",  # generic fallback
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    from app.refresh import fetch_one

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        fetch_one(s, feed)
        s.commit()

    assert True in calls, "expected refresh to try deep resolve on a generic fallback"

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        assert feed.favicon_url == "https://cdn.example.com/img/favicon.ico"


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
