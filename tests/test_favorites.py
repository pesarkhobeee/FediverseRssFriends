"""Favorites (★): toggle + list + visibility."""

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
    client.get(r.text[idx:end], follow_redirects=False)


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


def test_star_requires_login():
    _, item_id = _seed_feed_and_item()
    r = _client().post(f"/items/{item_id}/favorite")
    assert r.status_code == 401


def test_star_and_unstar():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")

    r = client.post(f"/items/{item_id}/favorite")
    assert r.status_code == 200
    assert "star-on" in r.text
    assert "★" in r.text

    r2 = client.delete(f"/items/{item_id}/favorite")
    assert r2.status_code == 200
    assert "star-on" not in r2.text
    assert "☆" in r2.text


def test_star_is_idempotent():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")

    client.post(f"/items/{item_id}/favorite")
    r = client.post(f"/items/{item_id}/favorite")  # again
    assert r.status_code == 200

    from sqlmodel import select

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        favs = s.exec(
            select(db_mod.Favorite)
            .where(db_mod.Favorite.feed_item_id == item_id)
        ).all()
    assert len(favs) == 1


def test_star_unknown_item_404():
    client = _client()
    _login(client, "alice@example.com")
    r = client.post("/items/99999/favorite")
    assert r.status_code == 404


def test_favorites_page_lists_starred_items():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")
    client.post(f"/items/{item_id}/favorite")

    r = client.get("/u/alice/favorites")
    assert r.status_code == 200
    assert "First post" in r.text
    assert "Example" in r.text
    assert "starred" in r.text.lower()


def test_favorites_page_empty():
    client = _client()
    _login(client, "alice@example.com")
    r = client.get("/u/alice/favorites")
    assert r.status_code == 200
    assert "Nothing starred yet" in r.text


def test_favorites_visibility_private_blocks_others():
    _, item_id = _seed_feed_and_item()
    owner = _client()
    _login(owner, "farid@example.com")
    owner.post(f"/items/{item_id}/favorite")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    anon = _client()
    assert anon.get("/u/farid/favorites").status_code == 403

    other = _client()
    _login(other, "someone@example.com")
    assert other.get("/u/farid/favorites").status_code == 403


def test_public_radar_shows_star_state_for_viewer():
    """When a signed-in viewer has already starred one of an owner's items,
    the star renders in the 'on' state on that owner's radar."""
    feed_id, item_id = _seed_feed_and_item()

    # Owner subscribes and viewer stars the item.
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
        s.add(db_mod.Favorite(user_id=viewer.id, feed_item_id=item_id))
        s.commit()

    client = _client()
    # Manually seed viewer session by going through /login.
    _login(client, "viewer@e.com")

    r = client.get("/u/owner")
    assert r.status_code == 200
    # The star should appear in the 'on' state next to the item.
    assert "star-on" in r.text
    assert "★" in r.text
