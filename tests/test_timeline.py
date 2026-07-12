"""Home timeline at /feed: auth gate, followee activity + own items,
interleave order, empty state, root redirect."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select


def _client():
    import app.main
    return TestClient(app.main.app)


def _login(client, email):
    r = client.post("/login", data={"email": email})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    client.get(r.text[idx:end], follow_redirects=False)


def test_feed_requires_login():
    r = _client().get("/feed")
    assert r.status_code == 401


def test_root_redirects_signed_in_to_own_page():
    client = _client()
    _login(client, "u@e.com")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    # Signed-in root goes to the user's own public page (handle derived
    # from email local-part).
    assert r.headers["location"] == "/u/u"


def test_root_anonymous_still_lands_on_signup():
    r = _client().get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Sign in with magic link" in r.text


def test_empty_feed_has_helpful_hint():
    client = _client()
    _login(client, "u@e.com")
    r = client.get("/feed")
    assert r.status_code == 200
    assert "Nothing here yet" in r.text


def test_feed_shows_followee_activity():
    """When A follows B and B stars an item, A sees the star event on /feed."""
    import app.db as db_mod

    # Seed B with a feed + item, and a favorite.
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Example")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="urn:1", title="Interesting post",
            link="https://x.example/1",
            published_at=datetime.now(UTC) - timedelta(hours=2),
        )
        s.add(item)
        s.commit()
        s.refresh(item)

    b = _client()
    _login(b, "b@e.com")
    b.post(f"/items/{item.id}/favorite")

    a = _client()
    _login(a, "a@e.com")
    a.post("/u/b/follow")

    r = a.get("/feed")
    assert r.status_code == 200
    assert "@b" in r.text
    assert "starred" in r.text
    assert "Interesting post" in r.text


def test_feed_shows_own_subscription_items():
    """User sees fresh items from their own subscriptions even without follows."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="My Blog")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.FeedItem(
            feed_id=feed.id, guid="p1", title="Fresh post",
            link="https://x.example/1",
            published_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        s.commit()

    client = _client()
    _login(client, "u@e.com")
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "u")).one()
        cat = db_mod.Category(user_id=user.id, name="Blogs", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        feed = s.exec(select(db_mod.Feed).where(db_mod.Feed.url == "https://x.example/feed")).one()
        s.add(db_mod.Subscription(
            user_id=user.id, feed_id=feed.id, category_id=cat.id, sort_order=1
        ))
        s.commit()

    r = client.get("/feed")
    assert r.status_code == 200
    assert "Fresh post" in r.text
    assert "My Blog" in r.text


def test_feed_shows_star_state_for_own_items():
    """A user's already-starred item on the timeline renders with ★ on."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Blog")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="p1", title="P1",
            published_at=datetime.now(UTC) - timedelta(hours=1),
        )
        s.add(item)
        s.commit()
        s.refresh(item)

    client = _client()
    _login(client, "u@e.com")
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "u")).one()
        cat = db_mod.Category(user_id=user.id, name="B", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        feed = s.exec(select(db_mod.Feed).where(db_mod.Feed.url == "https://x.example/feed")).one()
        s.add(db_mod.Subscription(
            user_id=user.id, feed_id=feed.id, category_id=cat.id, sort_order=1
        ))
        s.commit()

    # Star the item, then re-fetch the timeline.
    client.post(f"/items/{item.id}/favorite")
    r = client.get("/feed")
    assert "star-on" in r.text
    assert "★" in r.text
