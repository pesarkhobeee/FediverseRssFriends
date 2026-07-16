"""Self-service account deletion: two-step confirmation, cascade,
session teardown, and third-party row preservation."""

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


def test_dashboard_shows_danger_zone():
    c = _client()
    _login(c, "alice@e.com")
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "Danger zone" in r.text
    assert 'hx-get="/account/delete-confirm"' in r.text


def test_confirm_partial_requires_login():
    r = _client().get("/account/delete-confirm")
    assert r.status_code == 401


def test_confirm_partial_lists_footprint_and_disabled_button():
    c = _client()
    _login(c, "alice@e.com")
    r = c.get("/account/delete-confirm")
    assert r.status_code == 200
    assert "Type <strong>alice</strong>" in r.text
    assert 'id="delete-submit" disabled' in r.text
    assert "OPML" in r.text  # points them at the export before deletion


def test_delete_requires_matching_handle():
    c = _client()
    _login(c, "alice@e.com")
    r = c.post("/account/delete", data={"confirm_handle": "not-alice"})
    assert r.status_code == 400

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        u = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).first()
    assert u is not None, "account should still exist after a bad-handle attempt"


def test_delete_cascades_and_clears_session():
    """The user has categories, subs, faves, follows, activity — after
    delete, every row keyed on their id is gone; feeds are shared and
    stay put."""
    import app.db as db_mod

    alice = _client()
    _login(alice, "alice@e.com")
    bob = _client()
    _login(bob, "bob@e.com")

    # Seed a feed + subscription + favorite for alice, and a follow bob→alice.
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="X")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        alice_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        alice_id = alice_row.id
        cat = db_mod.Category(user_id=alice_id, name="c", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        s.add(db_mod.Subscription(
            user_id=alice_id, feed_id=feed.id, category_id=cat.id, sort_order=1
        ))
        item = db_mod.FeedItem(feed_id=feed.id, guid="g", title="t", link="l")
        s.add(item)
        s.commit()
        s.refresh(item)
        s.add(db_mod.Favorite(user_id=alice_id, feed_item_id=item.id))
        s.commit()
        feed_id = feed.id
        item_id = item.id

    bob.post("/u/alice/follow")

    # A third party (bob) has an Activity row where alice is the subject.
    import app.activity as act_mod
    with Session(db_mod.engine) as s:
        bob_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "bob")).one()
        act_mod.record(
            s, bob_row, "forked", "alice",
            subject_url="/u/alice", ref_type="user", ref_id=alice_id,
        )
        s.commit()

    r = alice.post("/account/delete", data={"confirm_handle": "alice"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?deleted=1"

    with Session(db_mod.engine) as s:
        # User + everything they owned is gone.
        def gone(model, col_name: str) -> bool:
            col = getattr(model, col_name)
            return s.exec(select(model).where(col == alice_id)).all() == []

        assert s.exec(select(db_mod.User).where(db_mod.User.id == alice_id)).first() is None
        assert gone(db_mod.Category, "user_id")
        assert gone(db_mod.Subscription, "user_id")
        assert gone(db_mod.Favorite, "user_id")
        assert gone(db_mod.Follow, "followee_id")
        assert gone(db_mod.Activity, "user_id")

        # Feeds + items survive — they're shared.
        assert s.exec(select(db_mod.Feed).where(db_mod.Feed.id == feed_id)).first() is not None
        surviving_item = s.exec(
            select(db_mod.FeedItem).where(db_mod.FeedItem.id == item_id)
        ).first()
        assert surviving_item is not None

        # Bob's "forked alice" activity row is preserved but its ref_id
        # is nulled — the subject text keeps the history readable.
        third_party = s.exec(
            select(db_mod.Activity).where(db_mod.Activity.subject == "alice")
        ).all()
        assert len(third_party) == 1
        assert third_party[0].ref_id is None
        assert third_party[0].subject == "alice"

    # The old session cookie no longer authenticates.
    r2 = alice.get("/dashboard")
    assert r2.status_code == 401

    # Bob is unaffected.
    assert bob.get("/dashboard").status_code == 200


def test_landing_shows_goodbye_after_delete():
    r = _client().get("/?deleted=1")
    assert r.status_code == 200
    assert "account was deleted" in r.text.lower()
