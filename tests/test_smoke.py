from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def test_index_renders():
    r = _client().get("/")
    assert r.status_code == 200
    assert "rssFriends" in r.text
    assert "Sign in with magic link" in r.text  # login form present


def test_healthz():
    r = _client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
