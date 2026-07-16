"""/f/{feed_id} feed page + POST /f/{id}/add + discover CTA state."""

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


def _seed_feed() -> int:
    """Return id of a seeded feed with one item."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://blog.example.com/feed.xml",
            title="Example Blog",
            site_url="https://blog.example.com",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.FeedItem(
            feed_id=feed.id, guid="post-1", title="First post",
            link="https://blog.example.com/1",
        ))
        s.commit()
        return feed.id


def test_feed_page_404_for_unknown():
    r = _client().get("/f/9999")
    assert r.status_code == 404


def test_anonymous_visitor_sees_sign_in_cta():
    feed_id = _seed_feed()
    r = _client().get(f"/f/{feed_id}")
    assert r.status_code == 200
    assert "Example Blog" in r.text
    assert "First post" in r.text
    assert "Sign in to add" in r.text
    # Inline login form on the page so they can start signing in.
    assert 'action="/login"' in r.text


def test_signed_in_new_user_sees_add_cta():
    feed_id = _seed_feed()
    client = _client()
    _login(client, "alice@example.com")
    r = client.get(f"/f/{feed_id}")
    assert r.status_code == 200
    assert "Add to my radar" in r.text
    # The button now opens the category picker instead of adding directly —
    # so users can pick a category or make a new one.
    assert f'hx-get="/f/{feed_id}/add-picker"' in r.text


def test_add_creates_discovered_category_and_subscription():
    feed_id = _seed_feed()
    client = _client()
    _login(client, "alice@example.com")

    r = client.post(f"/f/{feed_id}/add")
    assert r.status_code == 200
    assert "Following" in r.text

    from sqlmodel import Session

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).all()
        cats = s.exec(
            select(db_mod.Category).where(db_mod.Category.user_id == alice.id)
        ).all()
    assert len(subs) == 1
    assert subs[0].feed_id == feed_id
    assert [c.name for c in cats] == ["Discovered"]


def test_add_is_idempotent_no_duplicate_row():
    feed_id = _seed_feed()
    client = _client()
    _login(client, "alice@example.com")
    client.post(f"/f/{feed_id}/add")
    client.post(f"/f/{feed_id}/add")
    client.post(f"/f/{feed_id}/add")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).all()
    assert len(subs) == 1


def test_add_requires_login():
    feed_id = _seed_feed()
    r = _client().post(f"/f/{feed_id}/add")
    assert r.status_code == 401


def test_signed_in_already_subscribed_sees_in_radar_badge():
    feed_id = _seed_feed()
    client = _client()
    _login(client, "alice@example.com")
    client.post(f"/f/{feed_id}/add")

    r = client.get(f"/f/{feed_id}")
    assert r.status_code == 200
    assert "Following" in r.text
    assert "Add to my radar" not in r.text


def test_discover_popular_shows_state_aware_cta_per_row():
    """Anonymous sees "Sign in to add"
    a signed-in user who's subscribed
    to one of the popular feeds sees "Following" on THAT row and
    "Add to my radar" on the others."""
    # Seed two feeds and give each some subscribers via a shim user so
    # they appear on Discover.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        f1 = db_mod.Feed(url="https://a.example/feed", title="Feed One")
        f2 = db_mod.Feed(url="https://b.example/feed", title="Feed Two")
        s.add(f1)
        s.add(f2)
        s.commit()
        s.refresh(f1)
        s.refresh(f2)
        f1_id, f2_id = f1.id, f2.id
        seeder = db_mod.User(email="seeder@e.com", handle="seeder", visibility="public")
        s.add(seeder)
        s.commit()
        s.refresh(seeder)
        cat = db_mod.Category(user_id=seeder.id, name="c", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        for fid in (f1_id, f2_id):
            s.add(db_mod.Subscription(
                user_id=seeder.id, feed_id=fid, category_id=cat.id, sort_order=1
            ))
        s.commit()

    # Anonymous — every row shows "Sign in to add".
    r_anon = _client().get("/discover")
    assert r_anon.text.count("Sign in to add") >= 2

    # Signed-in, no subs — every row shows "Add to my radar".
    alice = _client()
    _login(alice, "alice@example.com")
    r_alice = alice.get("/discover")
    assert r_alice.text.count("Add to my radar") >= 2

    # After alice adds Feed One, only Feed Two still shows "Add".
    alice.post(f"/f/{f1_id}/add")
    r_after = alice.get("/discover")
    # The subscribed row now shows "Following" instead of the button.
    assert "Following" in r_after.text


def test_feed_page_subscriber_count_includes_everyone():
    """The subscriber total counts EVERY user regardless of visibility —
    aggregate count leaks no identifying info."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Test")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        # Mix of visibilities — all should be counted.
        for handle, vis in [
            ("pub1", "public"), ("pub2", "public"),
            ("un", "unlisted"), ("priv", "private"),
        ]:
            u = db_mod.User(email=f"{handle}@e.com", handle=handle, visibility=vis)
            s.add(u)
            s.commit()
            s.refresh(u)
            cat = db_mod.Category(user_id=u.id, name="c", sort_order=1)
            s.add(cat)
            s.commit()
            s.refresh(cat)
            s.add(db_mod.Subscription(
                user_id=u.id, feed_id=feed.id, category_id=cat.id, sort_order=1
            ))
        s.commit()
        feed_id = feed.id

    r = _client().get(f"/f/{feed_id}")
    assert "<strong>4</strong>" in r.text
    assert "subscribers on this instance" in r.text


def test_star_button_on_feed_page_for_signed_in():
    feed_id = _seed_feed()
    client = _client()
    _login(client, "alice@example.com")
    r = client.get(f"/f/{feed_id}")
    # Star button rendered next to the item, in the "off" state.
    assert "☆" in r.text
    assert 'hx-post="/items/' in r.text


def test_no_star_button_on_feed_page_for_anonymous():
    feed_id = _seed_feed()
    r = _client().get(f"/f/{feed_id}")
    assert "☆" not in r.text
    # Anonymous visitors see the items but can't star them.
    assert "First post" in r.text
