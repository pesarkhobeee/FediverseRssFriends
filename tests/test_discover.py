"""Discover page: sections + public-only filtering + rankings."""

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


def test_discover_open_to_anonymous():
    r = _client().get("/discover")
    assert r.status_code == 200
    assert "Recent radars" in r.text


def test_discover_empty_state():
    r = _client().get("/discover")
    assert r.status_code == 200
    assert "No public radars yet" in r.text
    assert "No feeds have been subscribed" in r.text
    assert "Nobody has been forked yet" in r.text


def test_recent_users_shows_only_public():
    a = _client()
    _login(a, "public@e.com")

    b = _client()
    _login(b, "unlisted@e.com")
    b.post("/settings/visibility", data={"visibility": "unlisted"}, follow_redirects=False)

    c = _client()
    _login(c, "private@e.com")
    c.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    r = _client().get("/discover")
    assert "@public" in r.text
    assert "@unlisted" not in r.text
    assert "@private" not in r.text


def test_popular_feeds_by_subscriber_count():
    """Two feeds; one has more subscribers → appears first."""
    a = _client()
    _login(a, "a@e.com")
    b = _client()
    _login(b, "b@e.com")

    # Seed two feeds and subscriptions directly.
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        popular = db_mod.Feed(url="https://pop.example/feed", title="Popular Feed")
        s.add(popular)
        niche = db_mod.Feed(url="https://niche.example/feed", title="Niche Feed")
        s.add(niche)
        s.commit()
        s.refresh(popular)
        s.refresh(niche)

        a_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "a")).one()
        b_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "b")).one()

        cat_a = db_mod.Category(user_id=a_row.id, name="c", sort_order=1)
        cat_b = db_mod.Category(user_id=b_row.id, name="c", sort_order=1)
        s.add(cat_a)
        s.add(cat_b)
        s.commit()
        s.refresh(cat_a)
        s.refresh(cat_b)

        # Both users subscribe to Popular Feed.
        s.add(db_mod.Subscription(
            user_id=a_row.id, feed_id=popular.id, category_id=cat_a.id, sort_order=1
        ))
        s.add(db_mod.Subscription(
            user_id=b_row.id, feed_id=popular.id, category_id=cat_b.id, sort_order=1
        ))
        # Only A subscribes to Niche Feed.
        s.add(db_mod.Subscription(
            user_id=a_row.id, feed_id=niche.id, category_id=cat_a.id, sort_order=2
        ))
        s.commit()

    r = _client().get("/discover")
    idx_pop = r.text.find("Popular Feed")
    idx_niche = r.text.find("Niche Feed")
    assert idx_pop > 0 and idx_niche > 0
    assert idx_pop < idx_niche, "popular feed should be listed first"
    assert "2 subscribers" in r.text
    assert "1 subscriber" in r.text


def test_popular_feeds_ignore_private_user_subs():
    """A private user's subscriptions don't leak into popularity counts."""
    a = _client()
    _login(a, "public@e.com")
    p = _client()
    _login(p, "private@e.com")
    p.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="A Feed")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        pu = s.exec(select(db_mod.User).where(db_mod.User.handle == "public")).one()
        pr = s.exec(select(db_mod.User).where(db_mod.User.handle == "private")).one()
        cat_pu = db_mod.Category(user_id=pu.id, name="c", sort_order=1)
        cat_pr = db_mod.Category(user_id=pr.id, name="c", sort_order=1)
        s.add(cat_pu)
        s.add(cat_pr)
        s.commit()
        s.refresh(cat_pu)
        s.refresh(cat_pr)
        s.add(db_mod.Subscription(
            user_id=pu.id, feed_id=feed.id, category_id=cat_pu.id, sort_order=1
        ))
        s.add(db_mod.Subscription(
            user_id=pr.id, feed_id=feed.id, category_id=cat_pr.id, sort_order=1
        ))
        s.commit()

    r = _client().get("/discover")
    # Only 1 (the public user), not 2.
    assert "1 subscriber" in r.text
    assert "2 subscribers" not in r.text


def test_most_forked_curators_ranking():
    """User with more fork activity events ranks first."""
    a = _client()
    _login(a, "a@e.com")
    # a has some feeds so fork actually copies things.
    r = a.post("/categories", data={"name": "News"})
    idx = r.text.find('id="category-') + len('id="category-')
    end = r.text.find('"', idx)
    cat_id = int(r.text[idx:end])

    # Give a a feed by inserting directly.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://a.example/feed", title="From A")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        a_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "a")).one()
        s.add(db_mod.Subscription(
            user_id=a_row.id, feed_id=feed.id, category_id=cat_id, sort_order=1
        ))
        s.commit()

    b = _client()
    _login(b, "b@e.com")
    b.post("/u/a/fork")

    c = _client()
    _login(c, "c@e.com")
    c.post("/u/a/fork")

    d = _client()
    _login(d, "d@e.com")
    # d also gets forked once by b:
    d.post("/categories", data={"name": "News"})
    with Session(db_mod.engine) as s:
        d_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "d")).one()
        feed2 = db_mod.Feed(url="https://d.example/feed", title="From D")
        s.add(feed2)
        s.commit()
        s.refresh(feed2)
        cat = s.exec(select(db_mod.Category).where(db_mod.Category.user_id == d_row.id)).first()
        s.add(db_mod.Subscription(
            user_id=d_row.id, feed_id=feed2.id, category_id=cat.id, sort_order=1
        ))
        s.commit()
    b.post("/u/d/fork")

    r = _client().get("/discover")
    # Both appear at least once (recent + forks lists), but the "forked N times"
    # numbers should reflect the counts.
    assert "forked 2 times" in r.text  # @a was forked twice
    assert "forked 1 time" in r.text   # @d was forked once
    # And @a's fork line appears before @d's in the "Most-forked" section.
    section = r.text[r.text.find("Most-forked"):]
    assert section.find("@a") < section.find("@d")
