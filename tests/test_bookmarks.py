"""Bookmarks (🔖): toggle + list + visibility."""

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


def _seed_feed_and_item():
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Example")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id,
            guid="urn:1",
            title="First post",
            link="https://x.example/1",
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        return feed.id, item.id


def test_bookmark_requires_login():
    _, item_id = _seed_feed_and_item()
    r = _client().post(f"/items/{item_id}/bookmark")
    assert r.status_code == 401


def test_bookmark_and_unbookmark():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")

    r = client.post(f"/items/{item_id}/bookmark")
    assert r.status_code == 200
    assert "bookmark-on" in r.text
    assert "🔖" in r.text

    r2 = client.delete(f"/items/{item_id}/bookmark")
    assert r2.status_code == 200
    assert "bookmark-on" not in r2.text
    assert "🏷" in r2.text


def test_bookmark_is_idempotent():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")

    client.post(f"/items/{item_id}/bookmark")
    r = client.post(f"/items/{item_id}/bookmark")  # again
    assert r.status_code == 200

    from sqlmodel import select

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        rows = s.exec(
            select(db_mod.Bookmark)
            .where(db_mod.Bookmark.feed_item_id == item_id)
        ).all()
    assert len(rows) == 1


def test_bookmark_unknown_item_404():
    client = _client()
    _login(client, "alice@example.com")
    r = client.post("/items/99999/bookmark")
    assert r.status_code == 404


def test_bookmarks_page_lists_saved_items():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")
    client.post(f"/items/{item_id}/bookmark")

    r = client.get("/u/alice/bookmarks")
    assert r.status_code == 200
    assert "First post" in r.text
    assert "Example" in r.text
    assert "bookmarked" in r.text.lower()


def test_bookmarks_page_empty():
    client = _client()
    _login(client, "alice@example.com")
    r = client.get("/u/alice/bookmarks")
    assert r.status_code == 200
    assert "You haven't bookmarked anything yet" in r.text


def test_bookmarks_visibility_private_blocks_others():
    _, item_id = _seed_feed_and_item()
    owner = _client()
    _login(owner, "farid@example.com")
    owner.post(f"/items/{item_id}/bookmark")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    anon = _client()
    assert anon.get("/u/farid/bookmarks").status_code == 403

    other = _client()
    _login(other, "someone@example.com")
    assert other.get("/u/farid/bookmarks").status_code == 403


def test_public_radar_shows_bookmark_state_for_viewer():
    """When a signed-in viewer has already bookmarked one of an owner's
    items, the bookmark renders in the 'on' state on that owner's radar."""
    feed_id, item_id = _seed_feed_and_item()

    # Owner subscribes and viewer bookmarks the item.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        owner = db_mod.User(email="owner@e.com", handle="owner")
        s.add(owner)
        viewer = db_mod.User(email="viewer@e.com", handle="viewer")
        s.add(viewer)
        s.commit()
        s.refresh(owner)
        s.refresh(viewer)
        cat = db_mod.Category(user_id=owner.id, name="News", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=feed_id, category_id=cat.id, sort_order=1
        ))
        s.add(db_mod.Bookmark(user_id=viewer.id, feed_item_id=item_id))
        s.commit()

    client = _client()
    _login(client, "viewer@e.com")

    r = client.get("/u/owner")
    assert r.status_code == 200
    assert "bookmark-on" in r.text
    assert "🔖" in r.text


def test_favorites_url_still_works_via_301():
    """Legacy /u/{handle}/favorites URL kept alive via 301 redirect for
    old bookmarks/links."""
    client = _client()
    _login(client, "alice@example.com")
    r = client.get("/u/alice/favorites", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/u/alice/bookmarks"


def test_legacy_item_favorite_endpoint_redirects():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")
    r = client.post(f"/items/{item_id}/favorite", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == f"/items/{item_id}/bookmark"
