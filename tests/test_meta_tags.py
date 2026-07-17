"""OpenGraph + Twitter Card meta tags on every rendered page. These
drive rich unfurls on Slack, Discord, Mastodon, iMessage, etc."""

from fastapi.testclient import TestClient


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


def _assert_baseline_og(html: str):
    assert '<meta property="og:site_name" content="rssFriends">' in html
    assert 'property="og:title"' in html
    assert 'property="og:description"' in html
    assert 'property="og:url"' in html
    assert 'property="og:image"' in html
    assert 'name="twitter:card"' in html
    assert 'name="twitter:title"' in html
    assert 'name="twitter:description"' in html
    assert 'name="twitter:image"' in html
    assert 'name="description"' in html


def test_landing_has_full_og_set():
    r = _client().get("/")
    assert r.status_code == 200
    _assert_baseline_og(r.text)
    # og:url reflects the actual URL (host + path).
    assert 'property="og:url" content="http://testserver/"' in r.text


def test_help_has_specific_description():
    r = _client().get("/help")
    _assert_baseline_og(r.text)
    assert "A short guide to rssFriends" in r.text


def test_user_radar_has_profile_og_and_custom_description(monkeypatch):
    c = _client()
    _login(c, "alice@e.com")
    r = _client().get("/u/alice")
    assert r.status_code == 200
    _assert_baseline_og(r.text)
    assert 'property="og:type" content="profile"' in r.text
    assert "@alice's radar on rssFriends" in r.text


def test_feed_page_uses_favicon_as_og_image():
    """A per-feed page overrides og:image with the feed's own favicon
    so link unfurls show the feed's identity."""
    from sqlmodel import Session

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://blog.example.com/feed",
            title="Example Blog",
            site_url="https://blog.example.com",
            favicon_url="https://blog.example.com/favicon.png",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    r = _client().get(f"/f/{feed_id}")
    assert r.status_code == 200
    _assert_baseline_og(r.text)
    assert 'property="og:type" content="article"' in r.text
    assert 'https://blog.example.com/favicon.png' in r.text
    assert "Example Blog on rssFriends" in r.text


def test_feed_page_without_favicon_falls_back_to_default_og_image():
    from sqlmodel import Session

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://noicon.example/feed",
            title="No Icon Blog",
            favicon_url="",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        feed_id = feed.id

    r = _client().get(f"/f/{feed_id}")
    assert "static/og-default.png" in r.text
