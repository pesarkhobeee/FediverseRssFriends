"""Admin invites while registration is closed."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import Invite, User, engine


def _client():
    import app.main
    return TestClient(app.main.app)


def _link(text: str) -> str | None:
    marker = "/auth/verify?token="
    idx = text.find(marker)
    if idx == -1:
        return None
    return text[idx:text.find('"', idx)].split("token=", 1)[1]


def _login(email: str) -> TestClient:
    client = _client()
    token = _link(client.post("/login", data={"email": email}).text)
    client.post("/auth/verify", data={"token": token})
    return client


def _closed(monkeypatch):
    import app.config
    monkeypatch.setattr(app.config, "OPEN_REGISTRATION", False)


def test_only_admin_sees_invite_page():
    admin = _login("root@example.com")  # first user = admin when ADMIN_HANDLES is empty
    other = _login("member@example.com")
    assert admin.get("/invite").status_code == 200
    assert other.get("/invite").status_code == 404
    assert other.post("/invite", data={"email": "x@example.com"}).status_code == 404
    assert _client().get("/invite", follow_redirects=False).status_code == 401
    assert "Open invites" in admin.get("/dashboard?panel=account").text
    assert "Open invites" not in other.get("/dashboard?panel=account").text


def test_invited_email_can_join_closed_instance(monkeypatch):
    admin = _login("root@example.com")
    _closed(monkeypatch)

    refused = _client().post("/login", data={"email": "friend@example.com"})
    assert _link(refused.text) is None
    assert "invite-only" in refused.text

    r = admin.post("/invite", data={"email": "Friend@Example.com"})
    assert "Invite sent to friend@example.com" in r.text
    assert r.headers["cache-control"] == "private, no-store"
    with Session(engine) as db:
        assert db.get(Invite, "friend@example.com") is not None
        assert db.exec(select(User).where(User.email == "friend@example.com")).first() is None
    assert "/u/friend" not in _client().get("/discover").text

    friend = _client()
    friend.post("/auth/verify", data={"token": _link(r.text)})
    assert friend.get("/dashboard").status_code == 200
    assert "joined as" in admin.get("/invite").text


def test_invite_survives_expired_link(monkeypatch):
    """The emailed link lasts minutes; the invite itself doesn't."""
    admin = _login("root@example.com")
    _closed(monkeypatch)
    admin.post("/invite", data={"email": "late@example.com"})
    later = _client().post("/login", data={"email": "late@example.com"})
    assert _link(later.text) is not None


def test_invite_rejects_existing_blocked_and_invalid(monkeypatch):
    import app.config

    admin = _login("root@example.com")
    monkeypatch.setattr(app.config, "BLOCKED_EMAIL_DOMAINS", frozenset({"spam.example"}))
    assert "already has an account" in admin.post(
        "/invite", data={"email": "root@example.com"}).text
    assert "domain is blocked" in admin.post(
        "/invite", data={"email": "bot@spam.example"}).text
    assert "look like an email" in admin.post(
        "/invite", data={"email": "nope"}).text
    with Session(engine) as db:
        assert db.exec(select(Invite)).all() == []


def test_account_list_shows_everyone_recent_login_first():
    admin = _login("root@example.com")
    for n in range(35):
        with Session(engine) as db:
            db.add(User(email=f"old{n}@example.com", handle=f"old{n}"))
            db.commit()
    _login("returning@example.com")
    body = admin.get("/invite").text
    assert "All accounts (37)" in body
    assert "@old0" in body and "@old34" in body
    assert body.index("@returning") < body.index("@old0")
