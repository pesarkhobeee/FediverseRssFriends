"""Tabbed panels on /dashboard and inline category rename."""

from fastapi.testclient import TestClient
from sqlmodel import Session


def _client():
    import app.main
    return TestClient(app.main.app)


def _login(client, email):
    r = client.post("/login", data={"email": email})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    token = r.text[idx:end].split("token=", 1)[1]
    client.post("/auth/verify", data={"token": token}, follow_redirects=False)


def _cat(client, name: str) -> int:
    r = client.post("/categories", data={"name": name})
    idx = r.text.find('id="category-') + len('id="category-')
    return int(r.text[idx:r.text.find('"', idx)])


# ------------- Panel dispatch -------------

def test_dashboard_default_panel_is_feeds():
    c = _client()
    _login(c, "alice@e.com")
    r = c.get("/dashboard")
    assert r.status_code == 200
    # Tab strip is present with all three panels...
    assert "📡 Feeds" in r.text
    assert "⚙️ Settings" in r.text
    assert "📥 Import / Export" in r.text
    # ...and the Feeds panel's content (subscribe form + category form) is rendered.
    assert "Create a category" in r.text
    assert "Subscribe to feeds" in r.text


def test_dashboard_settings_panel():
    c = _client()
    _login(c, "alice@e.com")
    r = c.get("/dashboard?panel=settings")
    assert r.status_code == 200
    assert "Visibility" in r.text
    assert "Danger zone" in r.text
    # Feeds-specific content is NOT rendered on this panel.
    assert "Create a category" not in r.text


def test_dashboard_import_panel():
    c = _client()
    _login(c, "alice@e.com")
    r = c.get("/dashboard?panel=import")
    assert r.status_code == 200
    assert "Import from OPML" in r.text
    assert "Export as OPML" in r.text


def test_dashboard_unknown_panel_falls_back_to_default():
    c = _client()
    _login(c, "alice@e.com")
    r = c.get("/dashboard?panel=bogus")
    assert r.status_code == 200
    assert "Create a category" in r.text  # feeds panel content


def test_htmx_tab_click_returns_body_with_tabstrip():
    """0.11.14: The HTMX tab-click response must INCLUDE the tab-strip
    inside the swap payload so .tab-active moves to the newly clicked
    tab (previously the strip lived outside #dashboard-body and never
    updated, so tabs looked stuck on whichever was active on first
    page load). Response is still just the body (not the full
    base.html shell) — HX-Target selects the swap zone."""
    c = _client()
    _login(c, "alice@e.com")
    r = c.get(
        "/dashboard?panel=settings",   # legacy alias, resolves to account
        headers={"HX-Request": "true", "HX-Target": "dashboard-body"},
    )
    assert r.status_code == 200
    # Tab-strip IS in the swap payload — that's the fix.
    assert 'class="tab-strip"' in r.text
    # And the account panel's content is present.
    assert "Visibility" in r.text
    # But it's still not a full page render (no <html> shell).
    assert "<html" not in r.text


# ------------- Category rename -------------

def test_rename_picker_requires_login():
    r = _client().get("/categories/1/rename-picker")
    assert r.status_code == 401


def test_rename_picker_shows_current_name():
    c = _client()
    _login(c, "alice@e.com")
    cid = _cat(c, "Weblogs")
    r = c.get(f"/categories/{cid}/rename-picker")
    assert r.status_code == 200
    assert 'value="Weblogs"' in r.text


def test_rename_updates_name():
    c = _client()
    _login(c, "alice@e.com")
    cid = _cat(c, "Old Name")
    r = c.post(f"/categories/{cid}/rename", data={"name": "New Name"})
    assert r.status_code == 204
    assert r.headers.get("HX-Refresh") == "true"

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        cat = s.get(db_mod.Category, cid)
    assert cat.name == "New Name"


def test_rename_trims_whitespace():
    c = _client()
    _login(c, "alice@e.com")
    cid = _cat(c, "Old")
    c.post(f"/categories/{cid}/rename", data={"name": "  Trimmed  "})
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        cat = s.get(db_mod.Category, cid)
    assert cat.name == "Trimmed"


def test_rename_rejects_empty():
    c = _client()
    _login(c, "alice@e.com")
    cid = _cat(c, "Old")
    r = c.post(f"/categories/{cid}/rename", data={"name": "   "})
    assert r.status_code == 400


def test_rename_rejects_duplicate_case_insensitive():
    """Category names are unique per-user, case-insensitively — same
    rule find_or_create_category uses."""
    c = _client()
    _login(c, "alice@e.com")
    _cat(c, "News")
    other_cid = _cat(c, "Weblogs")
    r = c.post(f"/categories/{other_cid}/rename", data={"name": "news"})
    assert r.status_code == 409

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        cat = s.get(db_mod.Category, other_cid)
    assert cat.name == "Weblogs"  # unchanged


def test_rename_same_name_case_change_allowed():
    """Renaming a category to itself with a case change (e.g. "news"
    → "News") isn't a duplicate — we exclude the row we're editing
    from the uniqueness check."""
    c = _client()
    _login(c, "alice@e.com")
    cid = _cat(c, "news")
    r = c.post(f"/categories/{cid}/rename", data={"name": "News"})
    assert r.status_code == 204

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        cat = s.get(db_mod.Category, cid)
    assert cat.name == "News"


def test_rename_rejects_foreign_category():
    alice = _client()
    _login(alice, "alice@e.com")
    bob = _client()
    _login(bob, "bob@e.com")
    bob_cid = _cat(bob, "Bobs")
    r = alice.post(f"/categories/{bob_cid}/rename", data={"name": "Hacked"})
    assert r.status_code == 404


def test_rename_button_appears_on_dashboard():
    c = _client()
    _login(c, "alice@e.com")
    cid = _cat(c, "Weblogs")
    r = c.get("/dashboard")
    assert f'hx-get="/categories/{cid}/rename-picker"' in r.text


def test_feeds_panel_has_collapse_all_and_prominent_refresh():
    c = _client()
    _login(c, "alice@e.com")
    r = c.get("/dashboard?panel=feeds")
    assert r.status_code == 200
    # Refresh is now the primary solid button, not a ghost.
    assert "🔄 Refresh now" in r.text
    # Collapse-all toggle is present.
    assert 'id="toggle-all-cats"' in r.text
    assert "Collapse all" in r.text
    # toggleAllCategories JS shipped inline.
    assert "toggleAllCategories" in r.text


def test_category_renders_as_details_wrapper():
    """Each category card is a native <details> so users can collapse
    it individually by clicking its header."""
    c = _client()
    _login(c, "alice@e.com")
    _cat(c, "Weblogs")
    r = c.get("/dashboard?panel=feeds")
    assert '<details class="card category"' in r.text
    assert 'class="category-summary"' in r.text
    # Categories start expanded (open) by default.
    assert 'category" id="category-' in r.text and " open>" in r.text


def test_category_name_endpoint_restores_header():
    """Cancel on the rename picker hits this to restore the default
    header without a full reload."""
    c = _client()
    _login(c, "alice@e.com")
    cid = _cat(c, "Weblogs")
    r = c.get(f"/categories/{cid}/name")
    assert r.status_code == 200
    assert "✏️ Rename" in r.text
    assert "Delete category" in r.text
    assert "Weblogs" in r.text
