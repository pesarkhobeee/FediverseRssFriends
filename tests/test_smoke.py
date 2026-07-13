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


def test_static_urls_carry_version_cachebuster():
    """CSS + favicon links include ?v={version} so a version bump busts
    Cloudflare's edge cache automatically."""
    from app import __version__
    r = _client().get("/")
    assert f"style.css?v={__version__}" in r.text
    assert f"favicon.svg?v={__version__}" in r.text
