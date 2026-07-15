"""/u/{handle}.rss — union of items across a user's subscriptions."""

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


def _seed_owner_with_two_items() -> None:
    """Owner subscribes to two feeds, each with one item at different times."""
    import app.db as db_mod

    o = _client()
    _login(o, "owner@e.com")
    r = o.post("/categories", data={"name": "News"})
    idx = r.text.find('id="category-') + len('id="category-')
    end = r.text.find('"', idx)
    cat_id = int(r.text[idx:end])

    with Session(db_mod.engine) as s:
        now = datetime.now(UTC)
        f_old = db_mod.Feed(url="https://a.example/feed", title="Feed A")
        f_new = db_mod.Feed(url="https://b.example/feed", title="Feed B")
        s.add(f_old)
        s.add(f_new)
        s.commit()
        s.refresh(f_old)
        s.refresh(f_new)

        s.add(db_mod.FeedItem(
            feed_id=f_old.id, guid="old-1", title="Old post",
            link="https://a.example/old", published_at=now - timedelta(days=2),
        ))
        s.add(db_mod.FeedItem(
            feed_id=f_new.id, guid="new-1", title="New post",
            link="https://b.example/new", published_at=now - timedelta(hours=1),
        ))
        owner = s.exec(select(db_mod.User).where(db_mod.User.handle == "owner")).one()
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=f_old.id, category_id=cat_id, sort_order=1
        ))
        s.add(db_mod.Subscription(
            user_id=owner.id, feed_id=f_new.id, category_id=cat_id, sort_order=2
        ))
        s.commit()


def test_public_reading_rss_is_open_and_well_formed():
    _seed_owner_with_two_items()
    r = _client().get("/u/owner.rss")
    assert r.status_code == 200
    assert "application/rss+xml" in r.headers["content-type"]
    assert "<rss" in r.text
    assert "@owner's reading list" in r.text
    # Both items present, newest first — "New post" appears before "Old post".
    idx_new = r.text.find("New post")
    idx_old = r.text.find("Old post")
    assert idx_new > 0 and idx_old > 0
    assert idx_new < idx_old
    # Each item tagged with its source feed.
    assert "Feed A" in r.text
    assert "Feed B" in r.text


def test_unknown_user_reading_rss_404():
    r = _client().get("/u/nobody.rss")
    assert r.status_code == 404


def test_private_reading_rss_blocks_anon_and_random():
    _seed_owner_with_two_items()
    o = _client()
    _login(o, "owner@e.com")
    o.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    assert _client().get("/u/owner.rss").status_code == 403

    other = _client()
    _login(other, "someone@e.com")
    assert other.get("/u/owner.rss").status_code == 403


def test_private_reading_rss_with_signed_token_works():
    _seed_owner_with_two_items()
    o = _client()
    _login(o, "owner@e.com")
    o.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    v = _client()
    _login(v, "viewer@e.com")
    v.post("/u/owner/follow")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        viewer_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one()
    o.post(f"/follow-requests/{viewer_row.id}/approve")

    from app.activity import sign_follower
    with Session(db_mod.engine) as s:
        owner_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "owner")).one()
        viewer_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one()
        token = sign_follower(owner_row, viewer_row)

    r = _client().get(f"/u/owner.rss?as=viewer&t={token}")
    assert r.status_code == 200
    assert "New post" in r.text


def test_reading_rss_bad_token_rejected():
    _seed_owner_with_two_items()
    o = _client()
    _login(o, "owner@e.com")
    o.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    v = _client()
    _login(v, "viewer@e.com")
    v.post("/u/owner/follow")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        viewer_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one()
    o.post(f"/follow-requests/{viewer_row.id}/approve")

    r = _client().get("/u/owner.rss?as=viewer&t=deadbeef")
    assert r.status_code == 403


def test_hero_shows_rss_link_for_visitor():
    _seed_owner_with_two_items()
    r = _client().get("/u/owner")
    assert r.status_code == 200
    # The visible button + the URL wired to the plain public feed.
    assert 'href="/u/owner.rss"' in r.text
    assert "🔗 RSS" in r.text


def test_hero_shows_signed_rss_link_for_approved_private_follower():
    """When @viewer is an approved follower of a private @owner, the
    hero link to /u/owner.rss should already include the signed token."""
    _seed_owner_with_two_items()
    o = _client()
    _login(o, "owner@e.com")
    o.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    v = _client()
    _login(v, "viewer@e.com")
    v.post("/u/owner/follow")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        viewer_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one()
    o.post(f"/follow-requests/{viewer_row.id}/approve")

    r = v.get("/u/owner")
    assert r.status_code == 200
    # Jinja escapes & → &amp; in the href attribute.
    assert "/u/owner.rss?as=viewer&amp;t=" in r.text


def test_reading_rss_does_not_collide_with_dot_opml_route():
    """Regression check: /u/{handle}.opml and /u/{handle}.rss are distinct."""
    _seed_owner_with_two_items()
    r_opml = _client().get("/u/owner.opml")
    r_rss = _client().get("/u/owner.rss")
    assert r_opml.status_code == 200
    assert "<opml" in r_opml.text
    assert r_rss.status_code == 200
    assert "<rss" in r_rss.text
