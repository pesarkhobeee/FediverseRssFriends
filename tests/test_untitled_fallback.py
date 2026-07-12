"""Mastodon-style titleless entries + backfill migration."""

import sqlite3

from sqlmodel import Session, select


def test_derive_title_from_summary_when_no_title():
    from app.refresh import _derive_title

    entry = {"title": "", "summary": "<p>Just a short toot with <b>HTML</b>.</p>"}
    got = _derive_title(entry)
    assert got == "Just a short toot with HTML."


def test_derive_title_falls_back_to_untitled_when_empty():
    from app.refresh import _derive_title

    assert _derive_title({"title": "", "summary": ""}) == "(untitled)"


def test_derive_title_keeps_original_when_present():
    from app.refresh import _derive_title

    assert _derive_title({"title": "Real title", "summary": "…"}) == "Real title"


def test_derive_title_truncates_long_summary():
    from app.refresh import _derive_title

    entry = {"title": "", "summary": "x" * 300}
    got = _derive_title(entry)
    assert got.endswith("…")
    assert len(got) <= 121  # 120 + ellipsis


MASTODON_ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Someone</title>
  <link href="https://mastodon.example/@someone"/>
  <entry>
    <id>tag:mastodon.example,2026:1</id>
    <link href="https://mastodon.example/@someone/1"/>
    <updated>2026-06-01T12:00:00Z</updated>
    <content type="html">&lt;p&gt;Hello from Mastodon!&lt;/p&gt;</content>
    <summary type="html">&lt;p&gt;Hello from Mastodon!&lt;/p&gt;</summary>
  </entry>
</feed>
"""


def test_full_fetch_gives_mastodon_entry_a_readable_title(monkeypatch):
    """End-to-end via fetch_one: a Mastodon-shaped entry ends up in the DB
    with a derived title, not the placeholder."""
    import app.refresh as ref

    class FakeResp:
        content = MASTODON_ATOM
        def raise_for_status(self): pass

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return FakeResp()

    monkeypatch.setattr(ref.httpx, "Client", FakeClient)

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://mastodon.example/@someone.atom", title="Someone")
        s.add(feed); s.commit(); s.refresh(feed)  # noqa: E702

    from app.refresh import fetch_one
    with Session(db_mod.engine) as s:
        feed = s.exec(select(db_mod.Feed).where(db_mod.Feed.url == "https://mastodon.example/@someone.atom")).one()
        new, err = fetch_one(s, feed)
    assert new == 1
    assert err is None

    with Session(db_mod.engine) as s:
        item = s.exec(select(db_mod.FeedItem)).one()
    assert item.title == "Hello from Mastodon!"


def test_migration_backfills_existing_untitled_items():
    """init_db() should retitle old (untitled) rows that DO have summaries."""
    import app.db as db_mod

    # Seed some legacy-looking rows directly.
    path = db_mod.engine.url.database
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            INSERT INTO feed (url, title, site_url, favicon_url,
                              consecutive_failures, last_error)
              VALUES ('https://z.example/feed', 'Z', '', '', 0, '');
            """
        )
        conn.commit()
        feed_id = conn.execute("SELECT id FROM feed WHERE url='https://z.example/feed'").fetchone()[0]
        # One row with an empty summary — should stay (untitled).
        conn.execute(
            "INSERT INTO feeditem (feed_id, guid, title, link, summary, fetched_at) "
            "VALUES (?, ?, '(untitled)', '', '', datetime('now'))",
            (feed_id, "empty-1"),
        )
        # One row with a summary — should get retitled.
        conn.execute(
            "INSERT INTO feeditem (feed_id, guid, title, link, summary, fetched_at) "
            "VALUES (?, ?, '(untitled)', '', '<p>A short excerpt here.</p>', datetime('now'))",
            (feed_id, "hasnote-1"),
        )
        conn.commit()
    finally:
        conn.close()

    db_mod.init_db()

    with Session(db_mod.engine) as s:
        empty = s.exec(select(db_mod.FeedItem).where(db_mod.FeedItem.guid == "empty-1")).one()
        hasnote = s.exec(select(db_mod.FeedItem).where(db_mod.FeedItem.guid == "hasnote-1")).one()
    assert empty.title == "(untitled)"
    assert hasnote.title == "A short excerpt here."
