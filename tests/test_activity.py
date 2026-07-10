"""Activity log: recording on write, timeline page, RSS + signed tokens."""

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


def _stub_resolve(monkeypatch):
    import app.dashboard
    import app.importers

    def fake(url):
        return {"title": f"Feed {url[-4:]}", "site_url": "https://x.example", "favicon_url": ""}

    monkeypatch.setattr(app.dashboard, "resolve_feed", fake)
    monkeypatch.setattr(app.importers, "resolve_feed", fake)


def _all_activity(user_id: int):
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        return s.exec(
            select(db_mod.Activity)
            .where(db_mod.Activity.user_id == user_id)
            .order_by(db_mod.Activity.created_at.asc())
        ).all()


def test_creating_category_records_activity():
    client = _client()
    _login(client, "u@e.com")
    client.post("/categories", data={"name": "News"})

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "u")).one()

    events = _all_activity(user.id)
    assert len(events) == 1
    assert events[0].kind == "created_category"
    assert events[0].subject == "News"


def test_subscribing_records_activity(monkeypatch):
    _stub_resolve(monkeypatch)
    client = _client()
    _login(client, "u@e.com")
    r = client.post("/categories", data={"name": "News"})
    idx = r.text.find('id="category-') + len('id="category-')
    end = r.text.find('"', idx)
    cat_id = int(r.text[idx:end])
    client.post("/feeds", data={"url": "https://x.example/feed", "category_id": cat_id})

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "u")).one()
    kinds = [e.kind for e in _all_activity(user.id)]
    assert "created_category" in kinds
    assert "subscribed" in kinds


def test_favoriting_records_activity():
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="F")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(feed_id=feed.id, guid="g", title="Post", link="https://x/1")
        s.add(item)
        s.commit()
        s.refresh(item)

    client = _client()
    _login(client, "u@e.com")
    client.post(f"/items/{item.id}/favorite")

    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "u")).one()
    events = _all_activity(user.id)
    assert any(e.kind == "favorited" and e.subject == "Post" for e in events)


def test_following_records_on_auto_approve():
    o = _client()
    _login(o, "owner@e.com")
    v = _client()
    _login(v, "viewer@e.com")
    v.post("/u/owner/follow")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        viewer_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one()
    assert any(e.kind == "followed" for e in _all_activity(viewer_row.id))


def test_following_recorded_on_approval_of_private_user():
    o = _client()
    _login(o, "owner@e.com")
    o.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    v = _client()
    _login(v, "viewer@e.com")
    v.post("/u/owner/follow")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        viewer_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one()
    # No activity yet — still pending.
    assert not any(e.kind == "followed" for e in _all_activity(viewer_row.id))

    o.post(f"/follow-requests/{viewer_row.id}/approve")
    assert any(e.kind == "followed" for e in _all_activity(viewer_row.id))


def test_activity_page_lists_events():
    client = _client()
    _login(client, "u@e.com")
    client.post("/categories", data={"name": "News"})
    r = client.get("/u/u/activity")
    assert r.status_code == 200
    assert "News" in r.text
    assert "created category" in r.text


def test_activity_page_private_blocks_others():
    o = _client()
    _login(o, "owner@e.com")
    o.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    assert _client().get("/u/owner/activity").status_code == 403


def test_activity_rss_public_open():
    client = _client()
    _login(client, "u@e.com")
    client.post("/categories", data={"name": "News"})

    r = _client().get("/u/u/activity.rss")
    assert r.status_code == 200
    assert "application/rss+xml" in r.headers["content-type"]
    assert "<rss" in r.text
    assert "created category" in r.text


def test_activity_rss_private_blocks_anon_and_random():
    o = _client()
    _login(o, "owner@e.com")
    o.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    assert _client().get("/u/owner/activity.rss").status_code == 403

    other = _client()
    _login(other, "someone@e.com")
    assert other.get("/u/owner/activity.rss").status_code == 403


def test_activity_rss_private_with_signed_token_works():
    """Approved follower can subscribe via signed URL with no session cookie."""
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

    # Mint token as the app does.
    from app.activity import sign_follower
    with Session(db_mod.engine) as s:
        owner_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "owner")).one()
        viewer_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one()
        token = sign_follower(owner_row, viewer_row)

    # A cookie-less client can now subscribe.
    anon = _client()
    r = anon.get(f"/u/owner/activity.rss?as=viewer&t={token}")
    assert r.status_code == 200
    assert "<rss" in r.text


def test_activity_rss_bad_token_rejected():
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

    r = _client().get("/u/owner/activity.rss?as=viewer&t=deadbeef1234abcd")
    assert r.status_code == 403


def test_revoking_follower_kills_rss_token_access():
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

    # Works while approved.
    assert _client().get(f"/u/owner/activity.rss?as=viewer&t={token}").status_code == 200

    # Owner revokes.
    o.post(f"/followers/{viewer_row.id}/revoke")

    # Token still verifies HMAC but the Follow row is gone → 403.
    assert _client().get(f"/u/owner/activity.rss?as=viewer&t={token}").status_code == 403
