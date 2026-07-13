"""URL normalization for GitHub and Reddit — on-ingest and one-shot migration."""

import sqlite3

from sqlmodel import Session, select


def test_normalize_github_profile_to_atom():
    from app.feeds import normalize_feed_url

    assert normalize_feed_url("https://github.com/schacon") == "https://github.com/schacon.atom"
    assert normalize_feed_url("https://github.com/dhh/") == "https://github.com/dhh.atom"


def test_normalize_leaves_github_atom_untouched():
    from app.feeds import normalize_feed_url

    assert normalize_feed_url("https://github.com/schacon.atom") == "https://github.com/schacon.atom"
    # Repo activity feeds stay untouched.
    long = "https://github.com/temporalio/helm-charts/releases.atom"
    assert normalize_feed_url(long) == long


def test_normalize_leaves_non_user_github_paths_untouched():
    from app.feeds import normalize_feed_url

    for u in [
        "https://github.com/orgs/nvidia",
        "https://github.com/marketplace",
        "https://github.com/topics/rust",
    ]:
        assert normalize_feed_url(u) == u


def test_normalize_reddit_subreddit_to_rss():
    from app.feeds import normalize_feed_url

    assert normalize_feed_url("https://www.reddit.com/r/rss/") == "https://www.reddit.com/r/rss/.rss"
    assert normalize_feed_url("https://reddit.com/r/rss") == "https://reddit.com/r/rss/.rss"
    assert normalize_feed_url("https://old.reddit.com/r/fountainpens/") == (
        "https://old.reddit.com/r/fountainpens/.rss"
    )


def test_normalize_leaves_reddit_rss_untouched():
    from app.feeds import normalize_feed_url

    assert normalize_feed_url("https://www.reddit.com/r/rss/.rss") == (
        "https://www.reddit.com/r/rss/.rss"
    )
    # Also don't touch reddit URLs with a sort suffix.
    assert normalize_feed_url("https://www.reddit.com/r/rss/top/") == (
        "https://www.reddit.com/r/rss/top/"
    )


def test_normalize_passthrough_for_regular_feeds():
    from app.feeds import normalize_feed_url

    assert normalize_feed_url("https://blog.example.com/feed.xml") == (
        "https://blog.example.com/feed.xml"
    )
    assert normalize_feed_url("") == ""


def test_migration_rewrites_legacy_feed_urls():
    """Existing Feed rows with a github.com/user profile URL get their url
    rewritten to the .atom form."""
    import app.db as db_mod

    path = db_mod.engine.url.database
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO feed (url, title, site_url, favicon_url, "
            "consecutive_failures, last_error) VALUES "
            "('https://github.com/schacon', 'schacon', '', '', 3, 'HTTP 404')"
        )
        conn.execute(
            "INSERT INTO feed (url, title, site_url, favicon_url, "
            "consecutive_failures, last_error) VALUES "
            "('https://www.reddit.com/r/rss/', 'rss subreddit', '', '', 5, 'blocked')"
        )
        conn.commit()
    finally:
        conn.close()

    db_mod.init_db()

    with Session(db_mod.engine) as s:
        urls = {f.url: f for f in s.exec(select(db_mod.Feed)).all()}
    assert "https://github.com/schacon.atom" in urls
    assert "https://github.com/schacon" not in urls
    assert "https://www.reddit.com/r/rss/.rss" in urls
    assert "https://www.reddit.com/r/rss/" not in urls
    # And the failure counters were reset since we're pointing at a new URL.
    assert urls["https://github.com/schacon.atom"].consecutive_failures == 0


def test_migration_merges_when_both_versions_exist():
    """If the .atom row already exists and someone subscribed to the profile
    URL too, the migration migrates the subscription over and drops the old
    Feed row without hitting the UNIQUE(user, feed) constraint."""
    import app.db as db_mod

    path = db_mod.engine.url.database
    conn = sqlite3.connect(path)
    try:
        # Two Feed rows for the same effective source.
        conn.execute(
            "INSERT INTO feed (url, title, site_url, favicon_url, "
            "consecutive_failures, last_error) VALUES "
            "('https://github.com/schacon', 'old', '', '', 0, '')"
        )
        conn.execute(
            "INSERT INTO feed (url, title, site_url, favicon_url, "
            "consecutive_failures, last_error) VALUES "
            "('https://github.com/schacon.atom', 'new', '', '', 0, '')"
        )
        # One user subscribed only to the OLD one.
        conn.execute(
            "INSERT INTO user (email, handle, visibility, activity_secret, created_at) "
            "VALUES ('u@e.com', 'u', 'public', 'secret1', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO category (user_id, name, sort_order) "
            "VALUES (1, 'GH', 1)"
        )
        # Look up feed ids.
        old_id = conn.execute(
            "SELECT id FROM feed WHERE url = 'https://github.com/schacon'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO subscription (user_id, feed_id, category_id, sort_order) "
            "VALUES (1, ?, 1, 1)",
            (old_id,),
        )
        conn.commit()
    finally:
        conn.close()

    db_mod.init_db()

    with Session(db_mod.engine) as s:
        feeds = s.exec(select(db_mod.Feed)).all()
        urls = [f.url for f in feeds]
        subs = s.exec(select(db_mod.Subscription)).all()

    # Only one Feed row remains — the .atom canonical one.
    assert urls == ["https://github.com/schacon.atom"]
    # The subscription moved over to it.
    assert len(subs) == 1
    assert subs[0].feed_id == feeds[0].id
