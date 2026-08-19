"""Radar page — stream view (merged newest-first item list).

Adds a `?view=stream` mode on /u/{handle} that flattens the user's
feeds into a single chronological list of items, paginated via
`?offset=N`. Toggle between By-feed / Stream lives in the radar
header and persists per-browser (JS-side, not tested here)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session


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


def _seed_owner_with_items(item_count: int = 3, category: str = "News"):
    """Create owner + one feed + one subscription + `item_count` items,
    each with a distinct published_at so ordering is deterministic."""
    from sqlmodel import select

    import app.db as db_mod

    o = _client()
    _login(o, "owner@example.com")
    r = o.post("/categories", data={"name": category})
    idx = r.text.find('id="category-') + len('id="category-')
    end = r.text.find('"', idx)
    cat_id = int(r.text[idx:end])

    with Session(db_mod.engine) as s:
        owner = s.exec(
            select(db_mod.User).where(db_mod.User.handle == "owner")
        ).one()
        feed = db_mod.Feed(url="https://x.example/feed", title="Example Feed")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=feed.id, category_id=cat_id, sort_order=1,
        ))
        base = datetime.now(UTC) - timedelta(days=1)
        for i in range(item_count):
            s.add(db_mod.FeedItem(
                feed_id=feed.id, guid=f"g{i}",
                title=f"Post number {i}",
                link=f"https://x.example/{i}",
                published_at=base + timedelta(hours=i),
            ))
        s.commit()
        return owner.id, feed.id, cat_id


def test_stream_view_lists_items_newest_first():
    _seed_owner_with_items(item_count=3)
    r = _client().get("/u/owner?view=stream")
    assert r.status_code == 200
    body = r.text
    # All 3 titles present.
    for i in range(3):
        assert f"Post number {i}" in body
    # Newest (i=2) appears before i=0 in the response.
    assert body.find("Post number 2") < body.find("Post number 0")


def test_stream_view_respects_category_filter():
    """Only items from feeds in the filtered category appear."""
    from sqlmodel import select

    import app.db as db_mod

    _seed_owner_with_items(item_count=2, category="News")
    # Add a second category + feed with distinct items.
    o = _client()
    _login(o, "owner@example.com")
    r = o.post("/categories", data={"name": "Games"})
    idx = r.text.find('id="category-') + len('id="category-')
    end = r.text.find('"', idx)
    games_cat_id = int(r.text[idx:end])
    with Session(db_mod.engine) as s:
        owner = s.exec(select(db_mod.User).where(db_mod.User.handle == "owner")).one()
        feed2 = db_mod.Feed(url="https://games.example/feed", title="Games Feed")
        s.add(feed2)
        s.commit()
        s.refresh(feed2)
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=feed2.id, category_id=games_cat_id, sort_order=2,
        ))
        s.add(db_mod.FeedItem(
            feed_id=feed2.id, guid="game-1",
            title="Games only post", link="https://games.example/1",
            published_at=datetime.now(UTC),
        ))
        s.commit()

    # Filter to News → the games post must NOT appear.
    r = _client().get("/u/owner?view=stream&cat=News")
    assert r.status_code == 200
    assert "Games only post" not in r.text
    assert "Post number 0" in r.text


def test_stream_toggle_renders_both_buttons_on_radar_header():
    _seed_owner_with_items(item_count=1)
    r = _client().get("/u/owner")
    body = r.text
    assert "radar-view-toggle" in body
    assert 'data-view="feeds"' in body
    assert 'data-view="stream"' in body
    # By default view=feeds is active.
    assert "radar-view-btn-active" in body


def test_stream_view_load_more_htmx_returns_partial(monkeypatch):
    """HX-Request with view=stream returns just the swap partial (rows
    + optional load-more sentinel), NOT the full page shell. This is
    what the "Load more items…" button triggers."""
    from app.public import STREAM_PAGE_SIZE

    _seed_owner_with_items(item_count=STREAM_PAGE_SIZE + 3)  # spill into a 2nd page
    r = _client().get(
        f"/u/owner?view=stream&offset={STREAM_PAGE_SIZE}",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.text
    # No <html> wrapper — partial only.
    assert "<!doctype html" not in body.lower()
    # First-page items should NOT appear; second-page items should.
    assert "Post number 0" in body                          # oldest (page 2)
    assert f"Post number {STREAM_PAGE_SIZE + 2}" not in body   # newest (page 1)


def test_stream_view_has_more_sentinel_only_when_needed():
    """Stream page shows a Load-more button iff there's another page."""
    from app.public import STREAM_PAGE_SIZE

    _seed_owner_with_items(item_count=STREAM_PAGE_SIZE - 1)
    r = _client().get("/u/owner?view=stream")
    # Under a page — no load-more.
    assert "stream-load-more" not in r.text


def test_stream_row_renders_expandable_summary_when_present():
    """0.11.44: items with a summary get a <details> block below the
    row so readers can peek at the excerpt without leaving the page.
    Rows without a summary stay compact — no <details> element."""
    from sqlmodel import Session, select

    import app.db as db_mod

    _seed_owner_with_items(item_count=1)
    with Session(db_mod.engine) as s:
        # Give the sole item a long summary so we exercise the truncated
        # <details> branch (excerpt + "Read more").
        it = s.exec(select(db_mod.FeedItem)).one()
        it.summary = "The quick brown fox jumps over the lazy dog. " * 30
        s.add(it)
        s.commit()

    r = _client().get("/u/owner?view=stream")
    assert r.status_code == 200
    assert "stream-row-summary" in r.text
    assert "Read more" in r.text


def test_stream_row_stays_compact_when_no_summary():
    _seed_owner_with_items(item_count=1)
    r = _client().get("/u/owner?view=stream")
    assert r.status_code == 200
    assert "stream-row-summary" not in r.text


def test_anonymous_nav_no_longer_shows_login_link():
    """0.11.44: 'Login' was removed from the nav — the landing page's
    signup form is the entry point. Brand link still routes to /."""
    r = _client().get("/discover")
    assert r.status_code == 200
    # Nav has Discover + Collections but not a Login item.
    assert ">Login<" not in r.text
    assert 'href="/#login"' not in r.text
