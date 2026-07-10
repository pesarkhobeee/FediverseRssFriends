"""Follow: auto-approve on public, pending on private, approve/reject,
followers/following pages, view-through-follow on private radars."""

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


def test_follow_public_user_auto_approves():
    owner = _client()
    _login(owner, "owner@e.com")

    viewer = _client()
    _login(viewer, "viewer@e.com")
    r = viewer.post("/u/owner/follow")
    assert r.status_code == 200
    assert "Following" in r.text

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        row = s.exec(select(db_mod.Follow)).first()
        assert row is not None
        assert row.approved_at is not None


def test_follow_private_user_is_pending():
    owner = _client()
    _login(owner, "owner@e.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    viewer = _client()
    _login(viewer, "viewer@e.com")
    r = viewer.post("/u/owner/follow")
    assert r.status_code == 200
    assert "Requested" in r.text

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        row = s.exec(select(db_mod.Follow)).one()
        assert row.approved_at is None


def test_cannot_follow_yourself():
    owner = _client()
    _login(owner, "owner@e.com")
    r = owner.post("/u/owner/follow")
    assert r.status_code == 400


def test_unfollow_removes_row():
    owner = _client()
    _login(owner, "owner@e.com")
    viewer = _client()
    _login(viewer, "viewer@e.com")
    viewer.post("/u/owner/follow")
    r = viewer.delete("/u/owner/follow")
    assert r.status_code == 200
    assert "Follow" in r.text  # button reverted

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        assert s.exec(select(db_mod.Follow)).first() is None


def test_approve_request_grants_access_to_private_radar():
    owner = _client()
    _login(owner, "owner@e.com")
    owner.post("/categories", data={"name": "News"})
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    viewer = _client()
    _login(viewer, "viewer@e.com")
    # Before follow: 403.
    assert viewer.get("/u/owner").status_code == 403
    viewer.post("/u/owner/follow")
    # Still pending → 403.
    assert viewer.get("/u/owner").status_code == 403

    # Owner approves.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        follower_id = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one().id

    r = owner.post(f"/follow-requests/{follower_id}/approve")
    assert r.status_code == 200

    # Now the viewer can see the private radar.
    assert viewer.get("/u/owner").status_code == 200


def test_reject_request_deletes_row():
    owner = _client()
    _login(owner, "owner@e.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    viewer = _client()
    _login(viewer, "viewer@e.com")
    viewer.post("/u/owner/follow")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        follower_id = s.exec(select(db_mod.User).where(db_mod.User.handle == "viewer")).one().id
    r = owner.post(f"/follow-requests/{follower_id}/reject")
    assert r.status_code == 200

    with Session(db_mod.engine) as s:
        assert s.exec(select(db_mod.Follow)).first() is None


def test_approve_nonexistent_request_404():
    owner = _client()
    _login(owner, "owner@e.com")
    r = owner.post("/follow-requests/99999/approve")
    assert r.status_code == 404


def test_follow_requests_inbox_lists_pending_only():
    owner = _client()
    _login(owner, "owner@e.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    a = _client()
    _login(a, "alice@e.com")
    a.post("/u/owner/follow")
    b = _client()
    _login(b, "bob@e.com")
    b.post("/u/owner/follow")

    r = owner.get("/follow-requests")
    assert r.status_code == 200
    assert "@alice" in r.text
    assert "@bob" in r.text


def test_public_radar_shows_counts_and_follow_button():
    owner = _client()
    _login(owner, "owner@e.com")

    viewer = _client()
    _login(viewer, "viewer@e.com")
    viewer.post("/u/owner/follow")

    r = viewer.get("/u/owner")
    assert r.status_code == 200
    assert "<strong>1</strong> follower" in r.text
    # Button reflects the following state on GET.
    assert "Following" in r.text


def test_followers_page():
    owner = _client()
    _login(owner, "owner@e.com")

    a = _client()
    _login(a, "alice@e.com")
    a.post("/u/owner/follow")
    b = _client()
    _login(b, "bob@e.com")
    b.post("/u/owner/follow")

    r = _client().get("/u/owner/followers")
    assert r.status_code == 200
    assert "@alice" in r.text
    assert "@bob" in r.text


def test_following_page():
    a = _client()
    _login(a, "alice@e.com")
    b = _client()
    _login(b, "bob@e.com")
    a.post("/u/bob/follow")

    r = _client().get("/u/alice/following")
    assert r.status_code == 200
    assert "@bob" in r.text


def test_dashboard_pending_count_shown():
    owner = _client()
    _login(owner, "owner@e.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    v = _client()
    _login(v, "viewer@e.com")
    v.post("/u/owner/follow")

    dash = owner.get("/dashboard").text
    assert "Follow requests (1)" in dash
