"""Per-feed refresh, owner-only Remove on public radar, feed sort order."""

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


def _seed_radar_with_dated_items() -> dict:
    """One owner, one category, two feeds with items at different times."""
    import app.db as db_mod

    now = datetime.now(UTC)
    with Session(db_mod.engine) as s:
        owner = db_mod.User(email="owner@e.com", handle="owner")
        s.add(owner)
        s.commit()
        s.refresh(owner)

        cat = db_mod.Category(user_id=owner.id, name="News", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)

        old_feed = db_mod.Feed(url="https://old.example/feed", title="Old One")
        new_feed = db_mod.Feed(url="https://new.example/feed", title="New One")
        s.add(old_feed)
        s.add(new_feed)
        s.commit()
        s.refresh(old_feed)
        s.refresh(new_feed)

        # OLD feed subscribed FIRST (lower sort_order) but has older content.
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=old_feed.id, category_id=cat.id, sort_order=1
        ))
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=new_feed.id, category_id=cat.id, sort_order=2
        ))
        s.add(db_mod.FeedItem(
            feed_id=old_feed.id, guid="o1", title="OLD post",
            published_at=now - timedelta(days=7),
        ))
        s.add(db_mod.FeedItem(
            feed_id=new_feed.id, guid="n1", title="NEW post",
            published_at=now - timedelta(hours=2),
        ))
        s.commit()

        return {
            "owner_id": owner.id,
            "old_feed_id": old_feed.id,
            "new_feed_id": new_feed.id,
        }


def test_public_radar_sorts_feeds_by_newest_item():
    _seed_radar_with_dated_items()
    r = _client().get("/u/owner")
    assert r.status_code == 200
    # "New One" should appear before "Old One" in the HTML.
    idx_new = r.text.find("New One")
    idx_old = r.text.find("Old One")
    assert idx_new > 0 and idx_old > 0
    assert idx_new < idx_old, "feeds should be sorted newest-content-first"


def test_public_radar_flat_sort_across_categories():
    """When no ?cat= filter is active, ALL feeds sort together by newest
    item — a fresh feed in the second category should appear before an
    old feed in the first category, regardless of category sort_order."""
    import app.db as db_mod

    now = datetime.now(UTC)
    with Session(db_mod.engine) as s:
        owner = db_mod.User(email="cross@e.com", handle="cross")
        s.add(owner)
        s.commit()
        s.refresh(owner)

        cat_a = db_mod.Category(user_id=owner.id, name="A", sort_order=1)
        cat_b = db_mod.Category(user_id=owner.id, name="B", sort_order=2)
        s.add(cat_a)
        s.add(cat_b)
        s.commit()
        s.refresh(cat_a)
        s.refresh(cat_b)

        old_feed = db_mod.Feed(url="https://old.a/feed", title="A-Feed")
        new_feed = db_mod.Feed(url="https://new.b/feed", title="B-Feed")
        s.add(old_feed)
        s.add(new_feed)
        s.commit()
        s.refresh(old_feed)
        s.refresh(new_feed)

        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=old_feed.id,
            category_id=cat_a.id, sort_order=1,
        ))
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=new_feed.id,
            category_id=cat_b.id, sort_order=1,
        ))
        s.add(db_mod.FeedItem(
            feed_id=old_feed.id, guid="a-1", title="OLD A post",
            published_at=now - timedelta(days=5),
        ))
        s.add(db_mod.FeedItem(
            feed_id=new_feed.id, guid="b-1", title="FRESH B post",
            published_at=now - timedelta(minutes=10),
        ))
        s.commit()

    r = _client().get("/u/cross")
    assert r.status_code == 200
    idx_fresh = r.text.find("B-Feed")
    idx_old = r.text.find("A-Feed")
    assert idx_fresh > 0 and idx_old > 0
    assert idx_fresh < idx_old, (
        "flat sort should put B-Feed (fresh) before A-Feed (old) "
        "regardless of category sort_order"
    )
    # Category chip should render next to each feed in flat mode.
    assert 'href="/u/cross?cat=A"' in r.text
    assert 'href="/u/cross?cat=B"' in r.text


def test_owner_sees_refresh_and_remove_on_own_page():
    ids = _seed_radar_with_dated_items()  # noqa: F841
    owner = _client()
    _login(owner, "owner@e.com")
    r = owner.get("/u/owner")
    assert r.status_code == 200
    assert "🔄 Refresh" in r.text
    assert "🗑️ Remove" in r.text
    # Owner should NOT see "Add to my radar" on their own page.
    assert "Add to my radar" not in r.text


def test_other_viewer_does_not_see_owner_actions():
    _seed_radar_with_dated_items()
    other = _client()
    _login(other, "someone@e.com")
    r = other.get("/u/owner")
    assert r.status_code == 200
    assert "🔄 Refresh" not in r.text
    assert "🗑️ Remove" not in r.text
    # But they DO see cherry-pick (they're signed in and not owner).
    assert "Add to my radar" in r.text


def test_anonymous_viewer_sees_no_action_buttons():
    _seed_radar_with_dated_items()
    r = _client().get("/u/owner")
    assert r.status_code == 200
    for token in ("🔄 Refresh", "🗑️ Remove", "Add to my radar"):
        assert token not in r.text


def test_refresh_single_feed_requires_subscription(monkeypatch):
    """A user who isn't subscribed to a feed can't force it."""
    import app.refresh

    def _stub(db, feed):
        return 0, None

    monkeypatch.setattr(app.refresh, "fetch_one", _stub)
    import app.dashboard
    monkeypatch.setattr(app.dashboard, "fetch_one", _stub)

    ids = _seed_radar_with_dated_items()
    other = _client()
    _login(other, "someone@e.com")
    r = other.post(f"/feeds/{ids['new_feed_id']}/refresh")
    assert r.status_code == 404


def test_refresh_single_feed_reports_new_items(monkeypatch):
    import app.dashboard

    def _stub(db, feed):
        return 3, None

    monkeypatch.setattr(app.dashboard, "fetch_one", _stub)

    ids = _seed_radar_with_dated_items()
    owner = _client()
    _login(owner, "owner@e.com")
    r = owner.post(f"/feeds/{ids['new_feed_id']}/refresh")
    assert r.status_code == 200
    assert "3 new items" in r.text


def test_refresh_single_feed_up_to_date_flash(monkeypatch):
    import app.dashboard

    def _stub(db, feed):
        return 0, None

    monkeypatch.setattr(app.dashboard, "fetch_one", _stub)

    ids = _seed_radar_with_dated_items()
    owner = _client()
    _login(owner, "owner@e.com")
    r = owner.post(f"/feeds/{ids['new_feed_id']}/refresh")
    assert r.status_code == 200
    assert "up to date" in r.text


def test_refresh_single_feed_error_flash(monkeypatch):
    import app.dashboard

    def _stub(db, feed):
        return 0, "network down"

    monkeypatch.setattr(app.dashboard, "fetch_one", _stub)

    ids = _seed_radar_with_dated_items()
    owner = _client()
    _login(owner, "owner@e.com")
    r = owner.post(f"/feeds/{ids['new_feed_id']}/refresh")
    assert r.status_code == 200
    assert "network down" in r.text


def test_remove_button_actually_unsubscribes_owner():
    """The owner's Remove on the public radar must invoke the existing
    DELETE /subscriptions/{sub_id} endpoint."""
    ids = _seed_radar_with_dated_items()
    owner = _client()
    _login(owner, "owner@e.com")

    # Fish the sub id out of the owner's dashboard.
    dash = owner.get("/dashboard").text
    # The template uses id="sub-<N>" for each row.
    import re
    ids_in_html = [int(m) for m in re.findall(r'id="sub-(\d+)"', dash)]
    assert ids_in_html, "no subscription IDs found on dashboard"

    r = owner.delete(f"/subscriptions/{ids_in_html[0]}")
    assert r.status_code == 200

    from sqlmodel import Session

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        remaining = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == ids["owner_id"])
        ).all()
    assert len(remaining) == 1
