"""Cherry-pick + fork from someone else's public radar."""

from fastapi.testclient import TestClient


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


def _stub_resolve(monkeypatch, **kw):
    import app.dashboard
    import app.importers

    def fake(url):
        return {
            "title": kw.get("title", f"Feed {url[-4:]}"),
            "site_url": kw.get("site_url", "https://x.example"),
            "favicon_url": kw.get("favicon_url", "https://x.example/favicon.ico"),
        }

    monkeypatch.setattr(app.dashboard, "resolve_feed", fake)
    monkeypatch.setattr(app.importers, "resolve_feed", fake)


def _seed_owner_radar(monkeypatch) -> tuple[TestClient, dict]:
    """Set up an 'owner' user with two categories and two feeds. Returns
    (client, {'news_cat': id, 'blog_cat': id, 'news_feed_id': int, 'blog_feed_id': int})."""
    _stub_resolve(monkeypatch)
    owner = _client()
    _login(owner, "owner@example.com")

    def _cat_id(name):
        r = owner.post("/categories", data={"name": name})
        marker = 'id="category-'
        idx = r.text.find(marker) + len(marker)
        end = r.text.find('"', idx)
        return int(r.text[idx:end])

    news = _cat_id("News")
    blogs = _cat_id("Blogs")
    owner.post("/feeds", data={"url": "https://a.example/feed", "category_id": news})
    owner.post("/feeds", data={"url": "https://b.example/feed", "category_id": blogs})

    from sqlmodel import Session, select

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feeds = {f.url: f.id for f in s.exec(select(db_mod.Feed)).all()}

    return owner, {
        "news_cat": news,
        "blog_cat": blogs,
        "news_feed_id": feeds["https://a.example/feed"],
        "blog_feed_id": feeds["https://b.example/feed"],
    }


def test_cherry_pick_requires_login(monkeypatch):
    _, ids = _seed_owner_radar(monkeypatch)
    anon = _client()
    r = anon.post(f"/u/owner/subscribe/{ids['news_feed_id']}")
    assert r.status_code == 401


def test_cherry_pick_copies_category(monkeypatch):
    _, ids = _seed_owner_radar(monkeypatch)
    viewer = _client()
    _login(viewer, "viewer@example.com")

    r = viewer.post(f"/u/owner/subscribe/{ids['news_feed_id']}")
    assert r.status_code == 200
    assert "Added" in r.text

    # Viewer now has a "News" category containing the feed.
    dash = viewer.get("/dashboard")
    assert "News" in dash.text  # category name appears
    assert "Feed" in dash.text  # feed title


def test_cherry_pick_second_time_reports_duplicate(monkeypatch):
    _, ids = _seed_owner_radar(monkeypatch)
    viewer = _client()
    _login(viewer, "v@example.com")
    viewer.post(f"/u/owner/subscribe/{ids['news_feed_id']}")
    r = viewer.post(f"/u/owner/subscribe/{ids['news_feed_id']}")
    assert r.status_code == 200
    assert "Already" in r.text


def test_cannot_pick_from_yourself(monkeypatch):
    owner, ids = _seed_owner_radar(monkeypatch)
    r = owner.post(f"/u/owner/subscribe/{ids['news_feed_id']}")
    assert r.status_code == 400


def test_fork_copies_categories_and_feeds(monkeypatch):
    _, ids = _seed_owner_radar(monkeypatch)  # noqa: F841
    viewer = _client()
    _login(viewer, "forker@example.com")

    r = viewer.post("/u/owner/fork")
    assert r.status_code == 200
    assert "<strong>2</strong> feeds added" in r.text
    assert "<strong>0</strong> already" in r.text

    from sqlmodel import Session, select

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        viewer_row = s.exec(
            select(db_mod.User).where(db_mod.User.handle == "forker")
        ).one()
        cats = {
            c.name for c in s.exec(
                select(db_mod.Category).where(db_mod.Category.user_id == viewer_row.id)
            ).all()
        }
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == viewer_row.id)
        ).all()
    assert cats == {"News", "Blogs"}
    assert len(subs) == 2


def test_fork_is_idempotent(monkeypatch):
    _seed_owner_radar(monkeypatch)
    viewer = _client()
    _login(viewer, "forker@example.com")

    viewer.post("/u/owner/fork")
    r = viewer.post("/u/owner/fork")
    assert "<strong>0</strong> feeds added" in r.text
    assert "<strong>2</strong> already" in r.text


def test_cannot_fork_yourself(monkeypatch):
    owner, _ = _seed_owner_radar(monkeypatch)
    r = owner.post("/u/owner/fork")
    assert r.status_code == 400


def test_private_owner_blocks_pick_and_fork(monkeypatch):
    owner, ids = _seed_owner_radar(monkeypatch)
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    viewer = _client()
    _login(viewer, "v@example.com")
    assert viewer.post(f"/u/owner/subscribe/{ids['news_feed_id']}").status_code == 404
    assert viewer.post("/u/owner/fork").status_code == 404
