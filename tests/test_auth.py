from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def _extract_token(dev_link: str) -> str:
    # dev link looks like http://testserver/auth/verify?token=XXXX
    return dev_link.rsplit("token=", 1)[1]


def test_dashboard_requires_login():
    r = _client().get("/dashboard")
    assert r.status_code == 401


def test_invalid_email_rejected(monkeypatch):
    r = _client().post("/login", data={"email": "not-an-email"})
    # Renders login_error, not a 400 — but still returns 200 with the message.
    assert r.status_code == 200
    assert "invalid email" in r.text


def test_login_verify_creates_user_and_signs_in(monkeypatch, capsys):
    client = _client()
    r = client.post("/login", data={"email": "Alice@Example.com"})
    assert r.status_code == 200
    # Dev mode: the link is rendered on the page.
    assert "/auth/verify?token=" in r.text

    # Pull the link out of the page.
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    link = r.text[idx:end]

    r2 = client.get(link, follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/dashboard"

    r3 = client.get("/dashboard")
    assert r3.status_code == 200
    assert "@alice" in r3.text  # handle derived from email
    assert "alice@example.com" in r3.text.lower()


def test_token_is_single_use(monkeypatch):
    client = _client()
    r = client.post("/login", data={"email": "bob@example.com"})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    link = r.text[idx:end]

    r1 = client.get(link, follow_redirects=False)
    assert r1.status_code == 303

    # A fresh, unauthenticated client should not be able to reuse the token.
    fresh = _client()
    r2 = fresh.get(link, follow_redirects=False)
    assert r2.status_code == 400


def test_logout_clears_session():
    client = _client()
    r = client.post("/login", data={"email": "carol@example.com"})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    client.get(r.text[idx:end], follow_redirects=False)

    assert client.get("/dashboard").status_code == 200
    client.get("/logout", follow_redirects=False)
    assert client.get("/dashboard").status_code == 401


def test_handle_collision_gets_suffix():
    client = _client()
    for email in ("dave@a.com", "dave@b.com"):
        r = client.post("/login", data={"email": email})
        marker = "/auth/verify?token="
        idx = r.text.find(marker)
        end = r.text.find('"', idx)
        client.get(r.text[idx:end], follow_redirects=False)
        client.get("/logout", follow_redirects=False)

    # Second user should be dave-2
    from sqlmodel import Session, select

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        handles = {u.handle for u in s.exec(select(db_mod.User)).all()}
    assert "dave" in handles
    assert "dave-2" in handles


def test_rate_limit_blocks_rapid_second_link(monkeypatch):
    import app.config
    monkeypatch.setattr(app.config, "MAGIC_LINK_MIN_INTERVAL_SECONDS", 60)
    client = _client()
    r1 = client.post("/login", data={"email": "eve@example.com"})
    assert r1.status_code == 200
    r2 = client.post("/login", data={"email": "eve@example.com"})
    assert r2.status_code == 200
    assert "please wait" in r2.text
