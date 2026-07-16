"""Every rendered page must include the current app version as the
`?v=...` cache-buster on static assets. Regression guard: previously
only main.py's templates instance had the `app_version` global set —
every other router served `?v=` empty, so browsers cached CSS forever."""

from fastapi.testclient import TestClient

from app import __version__


def _client():
    import app.main
    return TestClient(app.main.app)


def _assert_versioned(html: str):
    marker = f"style.css?v={__version__}"
    assert marker in html, f"missing {marker!r} in rendered HTML"


def test_discover_renders_versioned_css():
    r = _client().get("/discover")
    assert r.status_code == 200
    _assert_versioned(r.text)


def test_home_renders_versioned_css():
    r = _client().get("/")
    assert r.status_code == 200
    _assert_versioned(r.text)


def test_help_renders_versioned_css():
    r = _client().get("/help")
    assert r.status_code == 200
    _assert_versioned(r.text)
