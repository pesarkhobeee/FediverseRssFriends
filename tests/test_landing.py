"""Landing page (index.html) — new redesign structure + inline HTMX signup."""

import contextlib
import importlib
import os

from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def test_landing_hero_renders_with_new_structure():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.text
    assert "Your RSS," in body                 # h1
    assert "A social, RSS-friendly radar for the open web" in body  # eyebrow
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


# ─── 0.11.14: consent + email→handle transparency notice ──────────────


def test_landing_shows_email_to_handle_consent_notice():
    """Feedback: users didn't realize their email prefix becomes their
    handle. The landing page now surfaces this prominently before the
    signup button."""
    body = _client().get("/").text
    # Consent block is present with the key nouns.
    assert "public handle" in body
    assert "alice@example.com" in body   # illustrative example
    assert "@alice" in body
    # Consent is implicit: "By requesting a magic link you agree…"
    assert "By requesting a magic link" in body
    # Handle-preview element exists so the JS can fill it in.
    assert 'id="handle-preview"' in body


def test_contact_form_is_gone():
    """0.11.27: /contact was removed after it started attracting spam.
    Anyone who bookmarked the URL now gets a clean 404."""
    r = _client().get("/contact")
    assert r.status_code == 404
    r = _client().post("/contact", data={"message": "spam"})
    assert r.status_code == 405 or r.status_code == 404


# ─── 0.11.34: instance-branding + invite-only registration ────────────
#
# Both tests below temporarily change process-wide env vars that
# influence module-level config. To avoid leaking config into
# subsequent tests (config, templating, and every router module are
# import-time-evaluated), we wrap the mutation + reload cycle in a
# helper that restores + re-reloads on cleanup.


@contextlib.contextmanager
def _instance_env(**overrides: str):
    """Temporarily set RSSFRIENDS_* env vars, reload every module that
    reads them at import time, restore + reload on exit. Same pattern
    as test_activitypub.py's autouse fixture, extended to more modules
    because instance globals fan out through Jinja + router files."""
    prev = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    _reload_config_chain()
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reload_config_chain()


def _reload_config_chain() -> None:
    for name in (
        "app.config",
        "app.templating",
        "app.public",
        "app.dashboard",
        "app.collections",
        "app.discover",
        "app.follow",
        "app.timeline",
        "app.bookmarks",
        "app.feed_page",
        "app.personal_feeds",
        "app.activitypub",
        "app.main",
    ):
        importlib.reload(importlib.import_module(name))


def test_landing_shows_configured_instance_name():
    """Self-hosters set RSSFRIENDS_INSTANCE_NAME to their own brand.
    Everything template-facing (title, nav, meta, footer) should pick
    that up from the config knob, not the hardcoded 'rssFriends'."""
    with _instance_env(
        RSSFRIENDS_INSTANCE_NAME="closefriendfinder",
        RSSFRIENDS_INSTANCE_TAGLINE="A place to find close friends via RSS",
    ):
        from fastapi.testclient import TestClient

        import app.main as main_mod
        r = TestClient(main_mod.app).get("/")
        assert r.status_code == 200
        body = r.text
        assert "closefriendfinder" in body
        assert "A place to find close friends via RSS" in body
        assert 'class="brand">closefriendfinder' in body


def test_invite_only_mode_refuses_new_signups():
    """RSSFRIENDS_OPEN_REGISTRATION=0 → new emails get a clear
    'invite-only' error instead of a magic link."""
    with _instance_env(
        RSSFRIENDS_OPEN_REGISTRATION="0",
        RSSFRIENDS_INSTANCE_NAME="PrivateInstance",
    ):
        from fastapi.testclient import TestClient

        import app.main as main_mod
        c = TestClient(main_mod.app)
        r = c.post(
            "/login", data={"email": "new-person@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "invite-only" in r.text
        assert "Check your inbox" not in r.text
