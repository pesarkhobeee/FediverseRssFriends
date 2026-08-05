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
    assert "Sign in" in r.text
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
    """Anonymous sees "Sign in"
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

    # Anonymous — every row shows "Sign in".
    r_anon = _client().get("/discover")
    assert r_anon.text.count("Sign in") >= 2

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


def test_bookmark_button_on_feed_page_for_signed_in():
    feed_id = _seed_feed()
    client = _client()
    _login(client, "alice@example.com")
    r = client.get(f"/f/{feed_id}")
    # Bookmark button rendered next to the item, in the "off" state.
    assert "☆" in r.text
    assert 'hx-post="/items/' in r.text


def test_no_bookmark_button_on_feed_page_for_anonymous():
    feed_id = _seed_feed()
    r = _client().get(f"/f/{feed_id}")
    assert "☆" not in r.text
    # Anonymous visitors see the items but can't bookmark them.
    assert "First post" in r.text


# ─── 0.11.11: pagination on /f/{feed_id} ───────────────────────────────


def test_feed_page_paginates_at_30():
    """Seed 35 items on a feed; first page renders 30 with Load-more,
    HTMX GET at offset=30 returns the remaining 5 without the sentinel."""
    import app.db as db_mod

    feed_id = _seed_feed()  # already has 1 item "First post"
    with Session(db_mod.engine) as s:
        # Add 34 more so we have 35 total → 30 + 5 across two pages.
        for i in range(34):
            s.add(db_mod.FeedItem(
                feed_id=feed_id, guid=f"paged-{i}",
                title=f"paged-item-{i}", link=f"https://blog.example.com/p{i}",
            ))
        s.commit()

    r = _client().get(f"/f/{feed_id}")
    assert r.status_code == 200
    body = r.text
    # 35 total items (1 First post + 34 paged-item-*), 30 on page 1.
    # Items sort newest-first by fetched_at, so paged-item-33 is newest
    # and paged-item-4 is the 30th shown.
    assert "paged-item-33" in body
    assert "paged-item-4" in body
    # Bottom 5 are paged out: paged-item-3..0 and the original "First post".
    assert "paged-item-3<" not in body
    assert "First post" not in body
    assert 'id="feed-page-load-more"' in body
    assert "offset=30" in body

    # Page 2 as HTMX partial.
    r2 = _client().get(
        f"/f/{feed_id}?offset=30", headers={"HX-Request": "true"},
    )
    assert r2.status_code == 200
    body2 = r2.text
    assert "paged-item-3" in body2
    assert "First post" in body2   # oldest → last on page 2
    assert 'id="feed-page-load-more"' not in body2
    # And it's just the partial, not the full page chrome.
    assert "<html" not in body2
