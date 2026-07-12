"""Avatar resolution: GitHub owners, Mastodon channel images, YouTube,
site favicons — and the one-shot startup backfill."""

import sqlite3

from sqlmodel import Session, select


def test_github_owner_extracts_username():
    from app.feeds import github_owner

    assert github_owner("https://github.com/schacon") == "schacon"
    assert github_owner("https://github.com/schacon.atom") == "schacon"
    assert github_owner("https://github.com/torvalds/") == "torvalds"


def test_github_owner_works_for_repo_activity_feeds():
    from app.feeds import github_owner

    # The owner of a repo feed is the account, not the repo.
    assert github_owner(
        "https://github.com/temporalio/helm-charts/releases.atom"
    ) == "temporalio"
    assert github_owner(
        "https://github.com/trimstray/the-book-of-secret-knowledge/commits.atom"
    ) == "trimstray"


def test_github_owner_rejects_non_user_paths():
    from app.feeds import github_owner

    for url in [
        "https://github.com/orgs/nvidia",
        "https://github.com/topics/rust",
        "https://github.com/marketplace",
    ]:
        assert github_owner(url) is None


def test_github_owner_returns_none_for_non_github():
    from app.feeds import github_owner

    assert github_owner("https://example.com/user") is None
    assert github_owner("https://mastodon.social/@user") is None


def test_resolve_avatar_prefers_github_over_feed_image():
    from app.feeds import resolve_avatar

    fmeta = {"image": {"href": "https://cdn.example/whatever.png"}}
    got = resolve_avatar("https://github.com/dhh.atom", "https://github.com/dhh", fmeta)
    assert got == "https://github.com/dhh.png?size=64"


def test_resolve_avatar_uses_channel_image_when_available():
    from app.feeds import resolve_avatar

    fmeta = {"image": {"href": "https://mastodon.example/avatar/123.png"}}
    got = resolve_avatar(
        "https://mastodon.example/@someone.rss", "https://mastodon.example/@someone", fmeta
    )
    assert got == "https://mastodon.example/avatar/123.png"


def test_resolve_avatar_youtube_falls_back_to_site_favicon():
    from app.feeds import resolve_avatar

    got = resolve_avatar(
        "https://www.youtube.com/feeds/videos.xml?channel_id=X",
        "https://www.youtube.com/channel/X",
        feed_meta={},
    )
    assert got == "https://www.youtube.com/favicon.ico"


def test_resolve_avatar_generic_site_favicon():
    from app.feeds import resolve_avatar

    got = resolve_avatar("https://blog.example.com/feed.xml", "https://blog.example.com/", {})
    assert got == "https://blog.example.com/favicon.ico"


def _stub_html_fetch(monkeypatch, url_to_html: dict[str, str]) -> None:
    """Stub feeds._http_get_text to return canned HTML per URL."""
    import app.feeds as feeds

    def fake(url, timeout=3.0):
        return url_to_html.get(url)

    monkeypatch.setattr(feeds, "_http_get_text", fake)


def test_deep_resolves_youtube_channel_via_og_image(monkeypatch):
    from app.feeds import resolve_avatar

    channel_html = (
        '<html><head>'
        '<meta property="og:image" content="https://yt3.example/avatar.png">'
        '</head></html>'
    )
    _stub_html_fetch(monkeypatch, {
        "https://www.youtube.com/channel/UCabc123": channel_html,
    })

    url = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123"
    got = resolve_avatar(url, "https://www.youtube.com/channel/UCabc123", {}, deep=True)
    assert got == "https://yt3.example/avatar.png"


def test_shallow_resolve_skips_youtube_scrape():
    """Refresh cycle uses deep=False so we do NOT spend a round-trip on YT."""
    from app.feeds import resolve_avatar

    url = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123"
    got = resolve_avatar(url, "https://www.youtube.com/channel/UCabc123", {}, deep=False)
    assert got == "https://www.youtube.com/favicon.ico"


def test_deep_falls_back_to_link_rel_icon_html_parse(monkeypatch):
    from app.feeds import resolve_avatar

    site_html = (
        '<html><head>'
        '<link rel="shortcut icon" href="/static/icon-192.png">'
        '</head></html>'
    )
    _stub_html_fetch(monkeypatch, {"https://blog.example.com": site_html})

    got = resolve_avatar(
        "https://blog.example.com/feed.xml",
        "https://blog.example.com",
        {},
        deep=True,
    )
    assert got == "https://blog.example.com/static/icon-192.png"


def test_deep_link_rel_icon_absolute_href(monkeypatch):
    from app.feeds import resolve_avatar

    site_html = (
        '<html><head>'
        '<link href="https://cdn.example/icon.png" rel="icon">'
        '</head></html>'
    )
    _stub_html_fetch(monkeypatch, {"https://blog.example.com": site_html})

    got = resolve_avatar(
        "https://blog.example.com/feed.xml",
        "https://blog.example.com",
        {},
        deep=True,
    )
    assert got == "https://cdn.example/icon.png"


def test_deep_falls_all_the_way_through_to_favicon_ico(monkeypatch):
    """Every network step is stubbed to None; final fallback is /favicon.ico."""
    _stub_html_fetch(monkeypatch, {})

    from app.feeds import resolve_avatar
    got = resolve_avatar(
        "https://blog.example.com/feed",
        "https://blog.example.com",
        {},
        deep=True,
    )
    assert got == "https://blog.example.com/favicon.ico"


def test_migration_backfills_github_avatars():
    """Existing github.com Feed rows with generic favicons get upgraded."""
    import app.db as db_mod

    path = db_mod.engine.url.database
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO feed (url, title, site_url, favicon_url, "
            "consecutive_failures, last_error) VALUES "
            "('https://github.com/torvalds', 'torvalds', '', "
            " 'https://github.com/favicon.ico', 0, '')"
        )
        conn.execute(
            "INSERT INTO feed (url, title, site_url, favicon_url, "
            "consecutive_failures, last_error) VALUES "
            "('https://github.com/temporalio/helm-charts/releases.atom', "
            " 'helm', '', 'https://github.com/favicon.ico', 0, '')"
        )
        # Non-GitHub row shouldn't be touched.
        conn.execute(
            "INSERT INTO feed (url, title, site_url, favicon_url, "
            "consecutive_failures, last_error) VALUES "
            "('https://blog.example.com/feed', 'blog', '', "
            " 'https://blog.example.com/favicon.ico', 0, '')"
        )
        conn.commit()
    finally:
        conn.close()

    db_mod.init_db()

    with Session(db_mod.engine) as s:
        rows = {
            f.url: f.favicon_url
            for f in s.exec(select(db_mod.Feed)).all()
        }
    assert rows["https://github.com/torvalds"] == "https://github.com/torvalds.png?size=64"
    assert rows["https://github.com/temporalio/helm-charts/releases.atom"] == (
        "https://github.com/temporalio.png?size=64"
    )
    assert rows["https://blog.example.com/feed"] == "https://blog.example.com/favicon.ico"
