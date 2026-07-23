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
    token = r.text[idx:end].split("token=", 1)[1]

    client.post("/auth/verify", data={"token": token}, follow_redirects=False)


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
    assert "Send magic link" in r.text


def test_empty_feed_has_helpful_hint():
    client = _client()
    _login(client, "u@e.com")
    r = client.get("/feed")
    assert r.status_code == 200
    assert "Your Friends page is quiet" in r.text


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
    b.post(f"/items/{item.id}/bookmark")

    a = _client()
    _login(a, "a@e.com")
    a.post("/u/b/follow")

    r = a.get("/feed")
    assert r.status_code == 200
    assert "@b" in r.text
    assert "bookmarked" in r.text
    assert "Interesting post" in r.text


def test_feed_does_not_show_own_subscription_items():
    """Regression: the timeline is a social feed of followees' activity,
    NOT a merged reader for your own subscriptions. Fresh items from
    your subs live on /u/{handle} and /dashboard — not here."""
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
    # With no follows and no external activity, the timeline is empty
    # regardless of how many items the viewer's own feeds have.
    assert "Fresh post" not in r.text
    assert "Your Friends page is quiet" in r.text


def test_star_events_show_up_for_followers():
    """The social path: when @b stars an item and @a follows @b,
    @a's timeline shows "@b starred X" — the star is public."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Blog")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="p1", title="Great post",
            link="https://x.example/1",
            published_at=datetime.now(UTC) - timedelta(hours=1),
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        item_id = item.id

    b = _client()
    _login(b, "b@e.com")
    b.post(f"/items/{item_id}/bookmark")

    a = _client()
    _login(a, "a@e.com")
    a.post("/u/b/follow")

    r = a.get("/feed")
    assert "@b" in r.text
    assert "bookmarked" in r.text
    assert "Great post" in r.text
