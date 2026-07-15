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

    token = link.split("token=", 1)[1]
    r2 = client.post("/auth/verify", data={"token": token}, follow_redirects=False)
    assert r2.status_code == 303
    # Post-verify lands on the user's own public page (their handle).
    assert r2.headers["location"] == "/u/alice"

    r3 = client.get("/dashboard")
    assert r3.status_code == 200
    assert "@alice" in r3.text  # handle derived from email
    assert "alice@example.com" in r3.text.lower()


def test_get_verify_does_not_consume_token():
    """Gmail's link scanner pre-fetches URLs in emails. That's a GET.
    A GET on /auth/verify must NOT consume the single-use token, or the
    real user's click will be rejected. Regression for the bug that hit
    mertcangurcan on production."""
    client = _client()
    r = client.post("/login", data={"email": "eve@example.com"})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    link = r.text[idx:end]
    token = link.split("token=", 1)[1]

    # Simulate the scanner pre-fetching the link (GET, no POST).
    scanner = _client()
    r_scan = scanner.get(link, follow_redirects=False)
    assert r_scan.status_code == 200
    assert "Continue signing in" in r_scan.text  # confirmation page

    # The real user's POST from the confirmation button must still work.
    r_real = client.post("/auth/verify", data={"token": token}, follow_redirects=False)
    assert r_real.status_code == 303
    assert r_real.headers["location"] == "/u/eve"


def test_get_verify_expired_token_shows_friendly_page():
    """A dead token on GET renders the verify_error page (200), NOT the
    raw FastAPI JSON 400. The page includes a "get a new link" form."""
    r = _client().get("/auth/verify?token=nope-never-existed", follow_redirects=False)
    assert r.status_code == 200
    assert "no longer valid" in r.text
    assert 'action="/login"' in r.text


def test_token_is_single_use(monkeypatch):
    client = _client()
    r = client.post("/login", data={"email": "bob@example.com"})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    link = r.text[idx:end]

    token = link.split("token=", 1)[1]
    r1 = client.post("/auth/verify", data={"token": token}, follow_redirects=False)
    assert r1.status_code == 303

    # A fresh, unauthenticated client should not be able to reuse the token.
    # Instead of a 400 JSON, they get the friendly verify_error HTML page.
    fresh = _client()
    r2 = fresh.post("/auth/verify", data={"token": token}, follow_redirects=False)
    assert r2.status_code == 200
    assert "no longer valid" in r2.text


def test_logout_clears_session():
    client = _client()
    r = client.post("/login", data={"email": "carol@example.com"})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    token = r.text[idx:end].split("token=", 1)[1]

    client.post("/auth/verify", data={"token": token}, follow_redirects=False)

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
        token = r.text[idx:end].split("token=", 1)[1]

        client.post("/auth/verify", data={"token": token}, follow_redirects=False)
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
