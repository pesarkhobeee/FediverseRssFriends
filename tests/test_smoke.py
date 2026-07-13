from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def test_index_renders():
    r = _client().get("/")
    assert r.status_code == 200
    assert "rssFriends" in r.text
    assert "Sign in with magic link" in r.text  # login form present


def test_healthz_includes_version():
    from app import __version__
    r = _client().get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == __version__


def test_version_endpoint():
    from app import __version__
    r = _client().get("/version")
    assert r.status_code == 200
    assert r.json() == {"version": __version__}


def test_footer_shows_version():
    from app import __version__
    r = _client().get("/")
    assert f"rssFriends v{__version__}" in r.text
