"""Landing page (index.html) — new redesign structure + inline HTMX signup."""

from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def test_landing_hero_renders_with_new_structure():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.text
    assert "Your RSS," in body                 # h1
    assert "A social radar for the open web" in body  # eyebrow
    assert "Send magic link" in body           # inline signup CTA
    assert 'id="landing-signup"' in body       # HTMX target
    assert "landing-features" in body          # feature grid section
    assert "The open web, curated by people you like." in body  # dark CTA
    assert 'class="landing-page"' in body      # main gets unconstrained
    # Preview card should render its three seed rows.
    assert "Simon Willison's Weblog" in body
    assert "Hacker News: Best" in body
    assert "Mitchell Hashimoto" in body


def test_landing_inline_signup_htmx_returns_partial():
    """POST /login with HX-Request should return just the swap partial,
    not a full <html> page. The response replaces #landing-signup in
    place — user stays on the landing hero, no navigation."""
    r = _client().post(
        "/login",
        data={"email": "someone@example.com"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.text
    # Confirmation partial only — no <html>/<head> wrapper.
    assert "<!doctype html" not in body.lower()
    assert 'id="landing-signup"' in body
    assert "Check your inbox" in body
    assert "someone@example.com" in body


def test_landing_signup_non_htmx_still_serves_full_page():
    """Users with JS disabled fall back to the classic full-page POST."""
    r = _client().post(
        "/login",
        data={"email": "someone@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    # Full page — should have the <html> shell + the classic login_sent template.
    assert "<!doctype html" in r.text.lower()
    assert "Check your inbox" in r.text
