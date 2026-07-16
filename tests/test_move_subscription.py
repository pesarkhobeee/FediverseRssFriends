"""Moving a subscription between the user's own categories on /dashboard.
The plain UI action; underneath it's a Subscription.category_id update
with a fresh sort_order at the destination."""

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


def _seed_sub(user_handle: str, category_id: int, feed_url: str) -> int:
    """Directly insert a Subscription + Feed for the given user's category."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == user_handle)).one()
        feed = db_mod.Feed(url=feed_url, title="F")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        sub = db_mod.Subscription(
            user_id=user.id, feed_id=feed.id, category_id=category_id, sort_order=1
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return sub.id


def test_move_picker_requires_login():
    r = _client().get("/subscriptions/1/move-picker")
    assert r.status_code == 401


def test_move_picker_returns_current_users_categories_only():
    alice = _client()
    _login(alice, "alice@e.com")
    _make_cat(alice, "News")
    zines_id = _make_cat(alice, "Zines")
    bob = _client()
    _login(bob, "bob@e.com")
    _make_cat(bob, "Bobs-Category")

    sub_id = _seed_sub("alice", zines_id, "https://a.example/feed")

    r = alice.get(f"/subscriptions/{sub_id}/move-picker")
    assert r.status_code == 200
    assert "News" in r.text
    assert "Zines" in r.text
    assert "Bobs-Category" not in r.text
    # The current category is pre-selected.
    assert f'value="{zines_id}" selected' in r.text


def test_move_updates_category_and_returns_hx_refresh():
    alice = _client()
    _login(alice, "alice@e.com")
    src_id = _make_cat(alice, "Source")
    dst_id = _make_cat(alice, "Destination")
    sub_id = _seed_sub("alice", src_id, "https://a.example/feed")

    r = alice.post(f"/subscriptions/{sub_id}/move", data={"category_id": dst_id})
    assert r.status_code == 204
    # HTMX triggers a full-page refresh of /dashboard so both categories
    # re-render with the moved row in its new home.
    assert r.headers.get("HX-Refresh") == "true"

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        sub = s.get(db_mod.Subscription, sub_id)
    assert sub.category_id == dst_id


def test_move_to_end_of_destination_sort_order():
    alice = _client()
    _login(alice, "alice@e.com")
    src_id = _make_cat(alice, "Source")
    dst_id = _make_cat(alice, "Destination")

    # Seed 2 existing subs in destination so sort_order max = 2.
    _seed_sub("alice", dst_id, "https://d1.example/feed")
    _seed_sub("alice", dst_id, "https://d2.example/feed")
    moved_id = _seed_sub("alice", src_id, "https://m.example/feed")

    alice.post(f"/subscriptions/{moved_id}/move", data={"category_id": dst_id})

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        sub = s.get(db_mod.Subscription, moved_id)
    # Placed at the end of the destination — max prior sort_order was 1
    # (each _seed_sub uses sort_order=1), so the moved row lands at 2.
    assert sub.sort_order >= 2


def test_move_to_same_category_is_no_op():
    alice = _client()
    _login(alice, "alice@e.com")
    cat_id = _make_cat(alice, "Only")
    sub_id = _seed_sub("alice", cat_id, "https://a.example/feed")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        before = s.get(db_mod.Subscription, sub_id)
        before_order = before.sort_order

    r = alice.post(f"/subscriptions/{sub_id}/move", data={"category_id": cat_id})
    assert r.status_code == 204

    with Session(db_mod.engine) as s:
        after = s.get(db_mod.Subscription, sub_id)
    assert after.sort_order == before_order


def test_move_rejects_foreign_sub():
    alice = _client()
    _login(alice, "alice@e.com")
    alice_cat = _make_cat(alice, "Alices-Cat")

    bob = _client()
    _login(bob, "bob@e.com")
    bob_cat = _make_cat(bob, "Bobs-Cat")
    bob_sub_id = _seed_sub("bob", bob_cat, "https://b.example/feed")

    r = alice.post(f"/subscriptions/{bob_sub_id}/move", data={"category_id": alice_cat})
    assert r.status_code == 404


def test_move_rejects_foreign_target_category():
    alice = _client()
    _login(alice, "alice@e.com")
    alice_cat = _make_cat(alice, "Alices")
    alice_sub_id = _seed_sub("alice", alice_cat, "https://a.example/feed")

    bob = _client()
    _login(bob, "bob@e.com")
    bob_cat = _make_cat(bob, "Bobs")

    r = alice.post(f"/subscriptions/{alice_sub_id}/move", data={"category_id": bob_cat})
    assert r.status_code == 403

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        sub = s.get(db_mod.Subscription, alice_sub_id)
    assert sub.category_id == alice_cat  # unchanged


def test_actions_endpoint_restores_default_buttons():
    """Cancel on the move picker HITs this to restore the row's default
    action buttons without a page reload."""
    alice = _client()
    _login(alice, "alice@e.com")
    cat_id = _make_cat(alice, "News")
    sub_id = _seed_sub("alice", cat_id, "https://a.example/feed")

    r = alice.get(f"/subscriptions/{sub_id}/actions")
    assert r.status_code == 200
    assert "📁 Move" in r.text
    assert "Remove" in r.text


def test_move_button_appears_on_dashboard():
    alice = _client()
    _login(alice, "alice@e.com")
    cat_id = _make_cat(alice, "News")
    sub_id = _seed_sub("alice", cat_id, "https://a.example/feed")

    r = alice.get("/dashboard")
    assert f'hx-get="/subscriptions/{sub_id}/move-picker"' in r.text
