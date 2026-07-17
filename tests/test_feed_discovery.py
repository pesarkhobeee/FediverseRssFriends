"""Auto-discovery of feeds when a user pastes a non-feed URL.

Covers:
  - <link rel="alternate"> discovery
  - Common-path fallback probing
  - Single-candidate auto-promotion (no picker shown)
  - Multi-candidate → discovery picker returned
  - Zero candidates → 400 with a helpful message
  - POST /feeds/pick subscribes the checked URLs, silently skipping dupes
"""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.feeds import discover_feeds


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


def _cat(client, name: str) -> int:
    r = client.post("/categories", data={"name": name})
    idx = r.text.find('id="category-') + len('id="category-')
    return int(r.text[idx:r.text.find('"', idx)])


def _stub_discover(monkeypatch, candidates: list[dict]):
    """Replace discover_feeds everywhere it's imported so we exercise
    the routing logic without touching the network."""
    import app.dashboard as dash

    def fake(url):
        return list(candidates)

    monkeypatch.setattr(dash, "discover_feeds", fake)


def _stub_resolve_non_feed(monkeypatch):
    """Make resolve_feed report the URL as a non-feed so the dashboard
    triggers discovery instead of subscribing directly."""
    import app.dashboard as dash

    def fake(url):
        return {
            "title": url,
            "site_url": "",
            "favicon_url": "",
            "is_feed": False,
        }

    monkeypatch.setattr(dash, "resolve_feed", fake)


# ------------- discover_feeds itself -------------

def test_discover_extracts_link_rel_alternate(monkeypatch):
    import app.feeds as feeds_mod

    html = (
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" '
        'title="Main feed" href="/feed.xml">'
        '<link rel="alternate" type="application/atom+xml" '
        'title="Comments" href="https://blog.example.com/comments.atom">'
        '<link rel="stylesheet" href="/style.css">'  # not a feed
        '</head></html>'
    )
    monkeypatch.setattr(feeds_mod, "_http_get_text", lambda url, timeout=3.0: html)
    monkeypatch.setattr(feeds_mod, "_probe_candidate", lambda u: (True, ""))

    got = discover_feeds("https://blog.example.com")
    urls = [c["url"] for c in got]
    assert "https://blog.example.com/feed.xml" in urls
    assert "https://blog.example.com/comments.atom" in urls
    assert len(got) == 2
    types = {c["type"] for c in got}
    assert "rss" in types and "atom" in types


def test_discover_probes_common_paths_when_no_link_rel(monkeypatch):
    import app.feeds as feeds_mod

    monkeypatch.setattr(
        feeds_mod, "_http_get_text",
        lambda url, timeout=3.0: "<html><head></head><body>hi</body></html>",
    )

    def probe(url):
        return (url.endswith("/feed"), "Site Feed")

    monkeypatch.setattr(feeds_mod, "_probe_candidate", probe)

    got = discover_feeds("https://blog.example.com")
    assert len(got) == 1
    assert got[0]["url"] == "https://blog.example.com/feed"


def test_discover_skips_common_paths_when_input_is_subpath(monkeypatch):
    """Don't blindly hit /feed at the root when the user pasted a
    deep path — that would return a different site's feed."""
    import app.feeds as feeds_mod

    monkeypatch.setattr(
        feeds_mod, "_http_get_text",
        lambda url, timeout=3.0: "<html><head></head></html>",
    )
    monkeypatch.setattr(feeds_mod, "_probe_candidate", lambda u: (True, ""))

    got = discover_feeds("https://blog.example.com/some/deep/post")
    assert got == []


def test_discover_returns_empty_when_html_fetch_fails(monkeypatch):
    import app.feeds as feeds_mod

    monkeypatch.setattr(feeds_mod, "_http_get_text", lambda url, timeout=3.0: None)
    monkeypatch.setattr(feeds_mod, "_probe_candidate", lambda u: (False, ""))

    assert discover_feeds("https://noexist.example") == []


def test_discover_dedupes_by_normalized_url(monkeypatch):
    import app.feeds as feeds_mod

    html = (
        '<link rel="alternate" type="application/rss+xml" href="/feed">'
        '<link rel="alternate" type="application/rss+xml" href="/feed#section">'
        '<link rel="alternate" type="application/rss+xml" href="https://blog.example.com/feed">'
    )
    monkeypatch.setattr(feeds_mod, "_http_get_text", lambda url, timeout=3.0: html)
    monkeypatch.setattr(feeds_mod, "_probe_candidate", lambda u: (True, ""))

    got = discover_feeds("https://blog.example.com")
    urls = [c["url"] for c in got]
    assert len(urls) == 1  # deduped


# ------------- POST /feeds routing -------------

def test_subscribe_zero_candidates_returns_400(monkeypatch):
    _stub_resolve_non_feed(monkeypatch)
    _stub_discover(monkeypatch, [])

    c = _client()
    _login(c, "alice@e.com")
    cat_id = _cat(c, "News")
    r = c.post("/feeds", data={"url": "https://homepage.example", "category_id": cat_id})
    assert r.status_code == 400
    assert "Couldn't find an RSS feed" in r.text


def test_subscribe_single_candidate_auto_promotes(monkeypatch):
    """One hit → subscribe silently, no picker shown."""
    _stub_resolve_non_feed(monkeypatch)
    _stub_discover(monkeypatch, [
        {"url": "https://blog.example.com/feed.xml", "title": "The Feed", "type": "rss"},
    ])
    import app.dashboard as dash
    monkeypatch.setattr(
        dash, "_subscribe_to_url",
        lambda db, user, cat_id, url, meta=None: _real_subscribe(db, user, cat_id, url),
    )

    c = _client()
    _login(c, "alice@e.com")
    cat_id = _cat(c, "News")
    r = c.post("/feeds", data={"url": "https://blog.example.com", "category_id": cat_id})
    assert r.status_code == 200
    # A feed row was appended — not the discovery picker.
    assert "feed-discovery" not in r.text
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).all()
        assert len(subs) == 1
        feed = s.get(db_mod.Feed, subs[0].feed_id)
        assert feed.url == "https://blog.example.com/feed.xml"


def _real_subscribe(db, user, cat_id, url):
    """A stub for _subscribe_to_url that doesn't need network — used by
    tests that exercise the routing but don't want a real HTTP fetch."""
    import app.db as db_mod

    feed = db.exec(select(db_mod.Feed).where(db_mod.Feed.url == url)).first()
    if not feed:
        feed = db_mod.Feed(url=url, title=url, site_url="", favicon_url="")
        db.add(feed)
        db.commit()
        db.refresh(feed)
    sub = db_mod.Subscription(
        user_id=user.id, feed_id=feed.id, category_id=cat_id, sort_order=1
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub, feed


def test_subscribe_multiple_candidates_returns_picker(monkeypatch):
    _stub_resolve_non_feed(monkeypatch)
    _stub_discover(monkeypatch, [
        {"url": "https://a.example/feed", "title": "Main", "type": "rss"},
        {"url": "https://a.example/comments", "title": "Comments", "type": "rss"},
    ])

    c = _client()
    _login(c, "alice@e.com")
    cat_id = _cat(c, "News")
    r = c.post("/feeds", data={"url": "https://a.example", "category_id": cat_id})
    assert r.status_code == 200
    assert "feed-discovery" in r.text
    # HTMX retargets the swap so the picker lands in the discovery slot,
    # not appended to the feed <ul>.
    assert r.headers.get("HX-Retarget") == f"#discovery-{cat_id}"
    # Both candidates rendered as checkboxes.
    assert 'value="https://a.example/feed"' in r.text
    assert 'value="https://a.example/comments"' in r.text
    # Nothing subscribed yet.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        assert s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).all() == []


def test_subscribe_direct_feed_skips_discovery(monkeypatch):
    """When resolve_feed says the URL IS a feed, don't run discovery."""
    import app.dashboard as dash

    def resolve(url):
        return {"title": "T", "site_url": "", "favicon_url": "", "is_feed": True}

    monkeypatch.setattr(dash, "resolve_feed", resolve)

    called = []
    monkeypatch.setattr(dash, "discover_feeds", lambda u: called.append(u) or [])

    c = _client()
    _login(c, "alice@e.com")
    cat_id = _cat(c, "News")
    r = c.post(
        "/feeds", data={"url": "https://real-feed.example/rss", "category_id": cat_id}
    )
    assert r.status_code == 200
    assert called == []  # discovery never invoked


# ------------- POST /feeds/pick -------------

def test_pick_subscribes_selected_urls(monkeypatch):
    c = _client()
    _login(c, "alice@e.com")
    cat_id = _cat(c, "News")

    r = c.post("/feeds/pick", data={
        "category_id": cat_id,
        "urls": ["https://a.example/feed", "https://b.example/feed"],
    })
    assert r.status_code == 200

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).all()
    assert len(subs) == 2


def test_pick_dedupes_and_skips_existing(monkeypatch):
    """Same URL listed twice = one subscription. Second submit with the
    same set = zero new subscriptions (idempotent — no crash)."""
    c = _client()
    _login(c, "alice@e.com")
    cat_id = _cat(c, "News")

    r1 = c.post("/feeds/pick", data={
        "category_id": cat_id,
        "urls": ["https://a.example/feed", "https://a.example/feed"],
    })
    assert r1.status_code == 200

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).all()
    assert len(subs) == 1

    # Second submit — already subscribed, silently no-op.
    r2 = c.post("/feeds/pick", data={
        "category_id": cat_id,
        "urls": ["https://a.example/feed"],
    })
    assert r2.status_code == 200
    assert r2.text.strip() == ""  # nothing appended


def test_pick_rejects_foreign_category(monkeypatch):
    alice = _client()
    _login(alice, "alice@e.com")

    bob = _client()
    _login(bob, "bob@e.com")
    bob_cat = _cat(bob, "Bobs-News")

    r = alice.post("/feeds/pick", data={
        "category_id": bob_cat,
        "urls": ["https://a.example/feed"],
    })
    assert r.status_code == 404
