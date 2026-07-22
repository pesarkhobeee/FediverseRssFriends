"""PWA plumbing: manifest.webmanifest, /sw.js served at root scope with
version-pinned cache name, base.html wiring."""

import json

from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def test_manifest_served_as_json():
    r = _client().get("/static/manifest.webmanifest")
    assert r.status_code == 200
    payload = json.loads(r.text)
    assert payload["name"] == "rssFriends"
    assert payload["start_url"] == "/"
    assert payload["scope"] == "/"
    assert payload["display"] == "standalone"
    icons = {i["sizes"] + ":" + i["purpose"] for i in payload["icons"]}
    assert "192x192:any" in icons
    assert "512x512:any" in icons
    assert "512x512:maskable" in icons


def test_service_worker_served_at_root():
    """The SW must live at /sw.js so its scope covers the whole site;
    serving it from /static/sw.js would cap its scope to /static/*.
    Response must be JS-typed and carry Service-Worker-Allowed: /."""
    r = _client().get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers.get("service-worker-allowed") == "/"
    # Version substitution should have happened — the literal placeholder
    # must NOT survive to the client (otherwise cache would never bust).
    assert "__VERSION__" not in r.text
    import app
    assert app.__version__ in r.text


def test_service_worker_passthroughs_html():
    """A grep-level check that the SW does NOT try to cache HTML routes.
    We rely on 'stale-while-revalidate' + `/static/` scoping only —
    caching HTML would leak stale bookmark/follow state to users."""
    body = _client().get("/sw.js").text
    # The fetch handler explicitly early-returns for non-/static/ paths;
    # look for the pathname guard.
    assert '/static/' in body
    assert "url.pathname.startsWith" in body


def test_base_template_wires_pwa():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.text
    assert 'rel="manifest"' in body
    assert "manifest.webmanifest" in body
    assert 'rel="apple-touch-icon"' in body
    assert 'name="theme-color"' in body
    assert 'navigator.serviceWorker.register("/sw.js")' in body


def test_icons_are_reachable():
    for name in ("icon-192.png", "icon-512.png",
                 "icon-maskable-512.png", "apple-touch-icon.png"):
        r = _client().get(f"/static/icons/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 200  # sanity: not an empty file
