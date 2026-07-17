"""Owner can delete individual Activity events. Deletions propagate to
every surface that reads from Activity: /u/{handle}/activity, the
activity.rss feed, and any follower's /feed timeline."""

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


def _record_activity(handle: str, subject: str, kind: str = "subscribed") -> int:
    """Insert an activity row directly and return its id."""
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == handle)).one()
        ev = db_mod.Activity(
            user_id=user.id, kind=kind, subject=subject,
            subject_url=f"https://example/{subject}",
        )
        s.add(ev)
        s.commit()
        s.refresh(ev)
        return ev.id


def test_delete_requires_login():
    r = _client().delete("/activity/1")
    assert r.status_code == 401


def test_owner_can_delete_own_activity():
    c = _client()
    _login(c, "alice@e.com")
    ev_id = _record_activity("alice", "post-1")

    r = c.delete(f"/activity/{ev_id}")
    assert r.status_code == 200

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        assert s.get(db_mod.Activity, ev_id) is None


def test_cannot_delete_foreign_activity():
    alice = _client()
    _login(alice, "alice@e.com")

    bob = _client()
    _login(bob, "bob@e.com")
    ev_id = _record_activity("bob", "bobs-post")

    r = alice.delete(f"/activity/{ev_id}")
    assert r.status_code == 404

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        assert s.get(db_mod.Activity, ev_id) is not None  # still there


def test_delete_button_shows_only_for_owner():
    """The ✕ button appears on the activity page only when the viewer
    is the owner — followers/visitors don't see it."""
    owner = _client()
    _login(owner, "alice@e.com")
    ev_id = _record_activity("alice", "visible-post")

    r_owner = owner.get("/u/alice/activity")
    assert r_owner.status_code == 200
    assert f'hx-delete="/activity/{ev_id}"' in r_owner.text

    visitor = _client()
    _login(visitor, "bob@e.com")
    r_visitor = visitor.get("/u/alice/activity")
    assert r_visitor.status_code == 200
    assert f'hx-delete="/activity/{ev_id}"' not in r_visitor.text

    r_anon = _client().get("/u/alice/activity")
    assert r_anon.status_code == 200
    assert "hx-delete" not in r_anon.text


def test_deleted_activity_disappears_from_followers_timeline():
    """The whole point: after delete, followers no longer see it."""
    owner = _client()
    _login(owner, "alice@e.com")
    ev_id = _record_activity("alice", "regretted-post")

    follower = _client()
    _login(follower, "bob@e.com")
    follower.post("/u/alice/follow")

    r_before = follower.get("/feed")
    assert "regretted-post" in r_before.text

    owner.delete(f"/activity/{ev_id}")
    r_after = follower.get("/feed")
    assert "regretted-post" not in r_after.text


def test_deleted_activity_gone_from_activity_rss():
    owner = _client()
    _login(owner, "alice@e.com")
    ev_id = _record_activity("alice", "gone-from-rss")

    r_before = _client().get("/u/alice/activity.rss")
    assert "gone-from-rss" in r_before.text

    owner.delete(f"/activity/{ev_id}")
    r_after = _client().get("/u/alice/activity.rss")
    assert "gone-from-rss" not in r_after.text
