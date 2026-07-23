"""Public radar, visibility gate, OPML, and settings."""

from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def _login(client: TestClient, email: str) -> None:
    r = client.post("/login", data={"email": email})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    token = r.text[idx:end].split("token=", 1)[1]

    client.post("/auth/verify", data={"token": token}, follow_redirects=False)


def _extract_id(html: str, prefix: str) -> int:
    marker = f'id="{prefix}'
    idx = html.find(marker) + len(marker)
    end = html.find('"', idx)
    return int(html[idx:end])


def _stub_resolve(monkeypatch, title: str = "Example Feed", site: str = "https://example.com"):
    import app.dashboard

    def fake(url: str) -> dict:
        return {"title": title, "site_url": site, "favicon_url": f"{site}/favicon.ico"}

    monkeypatch.setattr(app.dashboard, "resolve_feed", fake)


def test_unknown_handle_404():
    r = _client().get("/u/nobody")
    assert r.status_code == 404


def test_owner_sees_welcome_card_on_empty_radar():
    """A new user on their own empty radar sees the getting-started card,
    NOT the bare 'radar is empty' message."""
    client = _client()
    _login(client, "newby@example.com")
    r = client.get("/u/newby")
    assert r.status_code == 200
    assert "Welcome" in r.text
    assert "Open Dashboard" in r.text
    assert "Open Discover" in r.text
    assert 'href="/help"' in r.text


def test_visitor_sees_plain_message_on_someone_elses_empty_radar():
    """Someone else's empty radar should NOT show the welcome card —
    that's just for the owner."""
    owner = _client()
    _login(owner, "farid@example.com")

    visitor = _client()
    r = visitor.get("/u/farid")
    assert r.status_code == 200
    assert "Welcome" not in r.text
    assert "This radar is empty" in r.text


def test_public_radar_shows_categories_and_feeds(monkeypatch):
    _stub_resolve(monkeypatch, title="Proton Blog")
    owner = _client()
    _login(owner, "farid@example.com")
    cat_id = _extract_id(owner.post("/categories", data={"name": "Weblogs"}).text, "category-")
    owner.post("/feeds", data={"url": "https://proton.me/blog/feed", "category_id": cat_id})

    visitor = _client()  # anonymous
    r = visitor.get("/u/farid")
    assert r.status_code == 200
    assert "@farid's radar" in r.text
    assert "Weblogs" in r.text
    assert "Proton Blog" in r.text
    # New radar header renders the count inline in the meta row:
    # `<strong>1</strong> feed`. See app/templates/user_radar.html.
    assert "<strong>1</strong> feed" in r.text


def test_feed_card_has_feed_page_and_copy_link_buttons(monkeypatch):
    """Every feed card on a user's radar exposes: the /f/{id} landing
    page (so visitors can share the feed itself, decoupled from any
    particular curator) and a copy-link button that JS turns into a
    clipboard write. The copy button is a plain <button> — no href —
    so we detect it by its data-copy-url attribute."""
    _stub_resolve(monkeypatch, title="Proton Blog")
    owner = _client()
    _login(owner, "farid@example.com")
    cat_id = _extract_id(owner.post("/categories", data={"name": "Weblogs"}).text, "category-")
    owner.post("/feeds", data={"url": "https://proton.me/blog/feed", "category_id": cat_id})
    page = _client().get("/u/farid").text
    assert '📄 Feed page' in page
    assert 'data-copy-url="/f/' in page
    # Feed-page link resolves to a working feed page.
    import re
    m = re.search(r'href="/f/(\d+)"', page)
    assert m, "expected an /f/{id} link on the radar"
    fid = int(m.group(1))
    assert _client().get(f"/f/{fid}").status_code == 200


def test_private_radar_blocks_others(monkeypatch):
    owner = _client()
    _login(owner, "priv@example.com")
    r = owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    assert r.status_code == 303

    # Owner can still see their own page.
    r_owner = owner.get("/u/priv")
    assert r_owner.status_code == 200

    # Anonymous visitor and another user cannot.
    anon = _client()
    assert anon.get("/u/priv").status_code == 403

    other = _client()
    _login(other, "someone@example.com")
    assert other.get("/u/priv").status_code == 403


def test_unlisted_is_viewable_by_link(monkeypatch):
    owner = _client()
    _login(owner, "hidden@example.com")
    owner.post("/settings/visibility", data={"visibility": "unlisted"}, follow_redirects=False)

    r = _client().get("/u/hidden")
    assert r.status_code == 200
    assert "unlisted" in r.text.lower()


def test_category_filter(monkeypatch):
    _stub_resolve(monkeypatch, title="Rustacean")
    owner = _client()
    _login(owner, "owner@example.com")
    news_id = _extract_id(owner.post("/categories", data={"name": "News"}).text, "category-")
    blog_id = _extract_id(owner.post("/categories", data={"name": "Blogs"}).text, "category-")
    owner.post("/feeds", data={"url": "https://n.example/feed", "category_id": news_id})
    owner.post("/feeds", data={"url": "https://b.example/feed", "category_id": blog_id})

    r_all = _client().get("/u/owner")
    assert r_all.text.count("Rustacean") >= 2  # appears in both categories

    r_news = _client().get("/u/owner?cat=News")
    assert r_news.status_code == 200
    # News category card renders (only news chip shows both categories in the chip bar).
    # Assert both chips are still present, and there's exactly one feed-card open shell.
    assert 'href="/u/owner?cat=News"' in r_news.text
    assert r_news.text.count('class="feed-card"') == 1


def test_opml_export(monkeypatch):
    _stub_resolve(monkeypatch, title="XKCD")
    owner = _client()
    _login(owner, "farid@example.com")
    cat_id = _extract_id(owner.post("/categories", data={"name": "Comics"}).text, "category-")
    owner.post("/feeds", data={"url": "https://xkcd.com/atom.xml", "category_id": cat_id})

    r = _client().get("/u/farid.opml")
    assert r.status_code == 200
    assert "text/x-opml" in r.headers["content-type"]
    assert "Comics" in r.text
    assert 'xmlUrl="https://xkcd.com/atom.xml"' in r.text
    assert 'title="XKCD"' in r.text


def test_opml_private_blocks_others():
    owner = _client()
    _login(owner, "farid@example.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    assert _client().get("/u/farid.opml").status_code == 403


def test_invalid_visibility_rejected():
    owner = _client()
    _login(owner, "farid@example.com")
    r = owner.post("/settings/visibility", data={"visibility": "🤷"}, follow_redirects=False)
    assert r.status_code == 400
