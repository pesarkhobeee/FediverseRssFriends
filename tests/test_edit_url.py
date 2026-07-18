"""Edit a subscription's feed URL from /dashboard.

Semantics — always subscription-scoped, never mutates other users' data:
  - same URL          → no-op
  - URL already has a Feed row + user has another sub to it → 409
  - URL already has a Feed row + user's current sub → migrate + orphan-gc
  - No Feed at new URL + no other subscribers → mutate old in place
  - No Feed at new URL + others subscribe → create new Feed row"""

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


def _make_cat(client, name: str) -> int:
    r = client.post("/categories", data={"name": name})
    idx = r.text.find('id="category-') + len('id="category-')
    return int(r.text[idx:r.text.find('"', idx)])


def _seed_sub(user_handle: str, cat_id: int, url: str, title: str = "F") -> tuple[int, int]:
    """Return (subscription_id, feed_id)."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == user_handle)).one()
        feed = db_mod.Feed(url=url, title=title, site_url="", favicon_url="")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        sub = db_mod.Subscription(
            user_id=user.id, feed_id=feed.id, category_id=cat_id, sort_order=1
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return sub.id, feed.id


def test_edit_url_picker_requires_login():
    r = _client().get("/subscriptions/1/edit-url-picker")
    assert r.status_code == 401


def test_edit_url_picker_shows_current_url():
    c = _client()
    _login(c, "alice@e.com")
    cat = _make_cat(c, "News")
    sub_id, _ = _seed_sub("alice", cat, "https://old.example/feed")

    r = c.get(f"/subscriptions/{sub_id}/edit-url-picker")
    assert r.status_code == 200
    assert 'value="https://old.example/feed"' in r.text
    assert 'name="url"' in r.text


def test_edit_url_same_url_is_no_op():
    c = _client()
    _login(c, "alice@e.com")
    cat = _make_cat(c, "News")
    sub_id, feed_id = _seed_sub("alice", cat, "https://old.example/feed")

    r = c.post(
        f"/subscriptions/{sub_id}/edit-url",
        data={"url": "https://old.example/feed"},
    )
    assert r.status_code == 204

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        sub = s.get(db_mod.Subscription, sub_id)
        feed = s.get(db_mod.Feed, feed_id)
    assert sub.feed_id == feed_id
    assert feed.url == "https://old.example/feed"


def test_edit_url_mutates_feed_in_place_when_no_other_subscribers():
    """Requested optimization: if no one else uses this Feed row, edit
    the URL in place instead of creating a new orphan-prone row."""
    c = _client()
    _login(c, "alice@e.com")
    cat = _make_cat(c, "News")
    sub_id, feed_id = _seed_sub(
        "alice", cat, "https://mistyped.example/feed", title="Old Title"
    )

    # Seed a stale item + favorite on the wrong feed.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        item = db_mod.FeedItem(feed_id=feed_id, guid="g1", title="stale")
        s.add(item)
        s.commit()
        s.refresh(item)
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        s.add(db_mod.Bookmark(user_id=alice.id, feed_item_id=item.id))
        s.commit()
        stale_item_id = item.id

    r = c.post(
        f"/subscriptions/{sub_id}/edit-url",
        data={"url": "https://correct.example/feed"},
    )
    assert r.status_code == 204
    assert r.headers.get("HX-Refresh") == "true"

    with Session(db_mod.engine) as s:
        # Same Feed row — but with the new URL and cleared metadata.
        feed = s.get(db_mod.Feed, feed_id)
        assert feed.url == "https://correct.example/feed"
        assert feed.title != "Old Title"        # reset for re-derive
        assert feed.last_fetched_at is None      # will refetch
        # Stale items and their favorites are gone (they were from the
        # wrong feed).
        assert s.get(db_mod.FeedItem, stale_item_id) is None
        # Only one Feed row exists — no orphan created.
        all_feeds = s.exec(select(db_mod.Feed)).all()
        assert len([f for f in all_feeds if f.id == feed_id]) == 1


def test_edit_url_creates_new_feed_when_others_still_subscribe():
    """If Bob still uses the old feed, Alice's edit must NOT mutate
    the row Bob depends on — a new Feed is created for Alice's URL."""
    alice = _client()
    _login(alice, "alice@e.com")
    a_cat = _make_cat(alice, "A")
    a_sub_id, shared_feed_id = _seed_sub("alice", a_cat, "https://shared.example/feed")

    bob = _client()
    _login(bob, "bob@e.com")
    b_cat = _make_cat(bob, "B")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        bob_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "bob")).one()
        s.add(db_mod.Subscription(
            user_id=bob_row.id, feed_id=shared_feed_id, category_id=b_cat, sort_order=1
        ))
        s.commit()

    alice.post(
        f"/subscriptions/{a_sub_id}/edit-url",
        data={"url": "https://alices-corrected.example/feed"},
    )

    with Session(db_mod.engine) as s:
        # Shared feed unchanged (Bob's world is safe).
        shared = s.get(db_mod.Feed, shared_feed_id)
        assert shared.url == "https://shared.example/feed"
        # Alice now points at a new Feed row.
        alice_sub = s.get(db_mod.Subscription, a_sub_id)
        assert alice_sub.feed_id != shared_feed_id
        new_feed = s.get(db_mod.Feed, alice_sub.feed_id)
        assert new_feed.url == "https://alices-corrected.example/feed"


def test_edit_url_reuses_existing_feed_row_and_orphan_gcs_old():
    """If the new URL already has a Feed row (from another user's
    subscription), reuse it. And if the old feed is now unsubscribed
    by everyone, drop it — no zero-subscriber orphans."""
    alice = _client()
    _login(alice, "alice@e.com")
    a_cat = _make_cat(alice, "A")
    a_sub_id, old_feed_id = _seed_sub("alice", a_cat, "https://old.example/feed")

    # A second Feed row already exists that Bob subscribes to.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        target = db_mod.Feed(url="https://new.example/feed", title="Target")
        s.add(target)
        s.commit()
        s.refresh(target)
        target_id = target.id
        bob_row = db_mod.User(email="bob@e.com", handle="bob", visibility="public")
        s.add(bob_row)
        s.commit()
        s.refresh(bob_row)
        b_cat = db_mod.Category(user_id=bob_row.id, name="B", sort_order=1)
        s.add(b_cat)
        s.commit()
        s.refresh(b_cat)
        s.add(db_mod.Subscription(
            user_id=bob_row.id, feed_id=target_id, category_id=b_cat.id, sort_order=1
        ))
        s.commit()

    r = alice.post(
        f"/subscriptions/{a_sub_id}/edit-url",
        data={"url": "https://new.example/feed"},
    )
    assert r.status_code == 204

    with Session(db_mod.engine) as s:
        alice_sub = s.get(db_mod.Subscription, a_sub_id)
        assert alice_sub.feed_id == target_id
        # Old feed had no other subscribers → dropped.
        assert s.get(db_mod.Feed, old_feed_id) is None


def test_edit_url_rejects_duplicate_subscription():
    c = _client()
    _login(c, "alice@e.com")
    cat = _make_cat(c, "N")
    sub_a_id, _ = _seed_sub("alice", cat, "https://a.example/feed")
    sub_b_id, _ = _seed_sub("alice", cat, "https://b.example/feed")

    # Try to edit sub_a's URL to point at sub_b's feed — user would
    # end up with two subscriptions to the same feed.
    r = c.post(
        f"/subscriptions/{sub_a_id}/edit-url",
        data={"url": "https://b.example/feed"},
    )
    assert r.status_code == 409


def test_edit_url_rejects_foreign_sub():
    alice = _client()
    _login(alice, "alice@e.com")

    bob = _client()
    _login(bob, "bob@e.com")
    b_cat = _make_cat(bob, "B")
    b_sub_id, _ = _seed_sub("bob", b_cat, "https://b.example/feed")

    r = alice.post(
        f"/subscriptions/{b_sub_id}/edit-url",
        data={"url": "https://hacked.example/feed"},
    )
    assert r.status_code == 404


def test_edit_url_button_on_dashboard():
    c = _client()
    _login(c, "alice@e.com")
    cat = _make_cat(c, "N")
    sub_id, _ = _seed_sub("alice", cat, "https://a.example/feed")

    r = c.get("/dashboard")
    assert f'hx-get="/subscriptions/{sub_id}/edit-url-picker"' in r.text
