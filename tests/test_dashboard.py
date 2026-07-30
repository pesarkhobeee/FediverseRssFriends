from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def _login(client: TestClient, email: str = "alice@example.com") -> None:
    r = client.post("/login", data={"email": email})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    token = r.text[idx:end].split("token=", 1)[1]

    client.post("/auth/verify", data={"token": token}, follow_redirects=False)


def _stub_resolve(monkeypatch, title: str = "Example Feed", site: str = "https://example.com"):
    import app.dashboard

    def fake(url: str) -> dict:
        return {"title": title, "site_url": site, "favicon_url": f"{site}/favicon.ico"}

    monkeypatch.setattr(app.dashboard, "resolve_feed", fake)


def test_dashboard_empty_shows_hint():
    client = _client()
    _login(client)
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "No categories yet" in r.text


def test_add_category_returns_partial():
    client = _client()
    _login(client)
    r = client.post("/categories", data={"name": "Weblogs"})
    assert r.status_code == 200
    assert "Weblogs" in r.text
    assert 'id="category-' in r.text

    # Now reflected on the dashboard as a category card.
    dash = client.get("/dashboard")
    assert 'class="category-name"' in dash.text
    assert "Weblogs" in dash.text
    # Empty-hint stays in the DOM (CSS hides it via :has()); presence of the
    # badge for a real category is our signal that the card rendered.
    assert 'class="category-badge"' in dash.text


def test_empty_category_name_rejected():
    client = _client()
    _login(client)
    r = client.post("/categories", data={"name": "   "})
    assert r.status_code == 400


def test_delete_category_cascades_subs(monkeypatch):
    _stub_resolve(monkeypatch)
    client = _client()
    _login(client)

    cat = client.post("/categories", data={"name": "News"})
    cat_id = _extract_id(cat.text, "category-")
    sub = client.post("/feeds", data={"url": "https://x.example/feed", "category_id": cat_id})
    assert sub.status_code == 200

    r = client.delete(f"/categories/{cat_id}")
    assert r.status_code == 200

    dash = client.get("/dashboard")
    assert "<h3 class=\"category-name\">News</h3>" not in dash.text
    assert "Example Feed" not in dash.text  # subscription gone with the category


def test_subscribe_adds_row(monkeypatch):
    _stub_resolve(monkeypatch, title="Proton Blog", site="https://proton.me")
    client = _client()
    _login(client)
    cat_id = _extract_id(client.post("/categories", data={"name": "Weblogs"}).text, "category-")

    r = client.post("/feeds", data={"url": "https://proton.me/blog/feed", "category_id": cat_id})
    assert r.status_code == 200
    assert "Proton Blog" in r.text
    assert 'id="sub-' in r.text


def test_duplicate_subscription_rejected(monkeypatch):
    _stub_resolve(monkeypatch)
    client = _client()
    _login(client)
    cat_id = _extract_id(client.post("/categories", data={"name": "News"}).text, "category-")

    client.post("/feeds", data={"url": "https://x.example/feed", "category_id": cat_id})
    r = client.post("/feeds", data={"url": "https://x.example/feed", "category_id": cat_id})
    assert r.status_code == 409


def test_unsubscribe_removes_row(monkeypatch):
    _stub_resolve(monkeypatch)
    client = _client()
    _login(client)
    cat_id = _extract_id(client.post("/categories", data={"name": "News"}).text, "category-")
    sub = client.post("/feeds", data={"url": "https://x.example/feed", "category_id": cat_id})
    sub_id = _extract_id(sub.text, "sub-")

    r = client.delete(f"/subscriptions/{sub_id}")
    assert r.status_code == 200

    dash = client.get("/dashboard")
    assert "Example Feed" not in dash.text


def test_cannot_touch_other_users_category(monkeypatch):
    _stub_resolve(monkeypatch)
    alice = _client()
    _login(alice, "alice@example.com")
    cat_id = _extract_id(alice.post("/categories", data={"name": "Alice's"}).text, "category-")

    bob = _client()
    _login(bob, "bob@example.com")
    assert bob.delete(f"/categories/{cat_id}").status_code == 404
    r = bob.post("/feeds", data={"url": "https://x.example/feed", "category_id": cat_id})
    assert r.status_code == 404


def test_feeds_are_deduplicated(monkeypatch):
    _stub_resolve(monkeypatch)
    alice = _client()
    _login(alice, "alice@example.com")
    a_cat = _extract_id(alice.post("/categories", data={"name": "A"}).text, "category-")
    alice.post("/feeds", data={"url": "https://x.example/feed", "category_id": a_cat})

    bob = _client()
    _login(bob, "bob@example.com")
    b_cat = _extract_id(bob.post("/categories", data={"name": "B"}).text, "category-")
    bob.post("/feeds", data={"url": "https://x.example/feed", "category_id": b_cat})

    from sqlmodel import Session, select

    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feeds = s.exec(select(db_mod.Feed)).all()
        subs = s.exec(select(db_mod.Subscription)).all()
    assert len(feeds) == 1  # one shared Feed row
    assert len(subs) == 2  # two subscriptions to it


def _extract_id(html: str, prefix: str) -> int:
    marker = f'id="{prefix}'
    idx = html.find(marker) + len(marker)
    end = html.find('"', idx)
    return int(html[idx:end])


def test_subscribe_shows_fetched_confirmation_chip(monkeypatch):
    """After adding a feed, the returned row should carry a
    "✓ Fetched N items" chip so the user has immediate feedback
    that the feed actually delivers content."""
    import app.dashboard

    _stub_resolve(monkeypatch, title="Test Blog", site="https://blog.example")

    # Override the autouse no-op stub for THIS test: return a real
    # fetched-count so the chip renders with a number.
    def fake_fetch(db, feed):
        # Simulate: 7 items fetched. Bump last_fetched_at on the row
        # so we behave like the real fetch_one.
        from datetime import UTC, datetime
        feed.last_fetched_at = datetime.now(UTC)
        db.add(feed)
        db.commit()
        return 7

    monkeypatch.setattr(
        app.dashboard, "_fetch_inline_for_new_subscription", fake_fetch,
    )

    client = _client()
    _login(client, "alice@example.com")
    r = client.post("/categories", data={"name": "News"})
    cat_id = _extract_id(r.text, "category-")
    r = client.post(
        "/feeds",
        data={"url": "https://blog.example/rss.xml", "category_id": cat_id},
    )
    assert r.status_code == 200
    assert "feed-just-added-chip" in r.text
    assert "Fetched 7 items" in r.text


def test_subscribe_shows_warning_chip_on_fetch_failure(monkeypatch):
    """When the inline fetch returns 0 items AND the feed row has
    a consecutive_failures bump, show a warning-styled chip that
    reassures the user the background refresh will retry."""
    import app.dashboard

    _stub_resolve(monkeypatch, title="Broken Blog", site="https://broken.example")

    def fake_fetch_fail(db, feed):
        # Simulate a fetch that failed and journaled the failure.
        feed.consecutive_failures = 1
        feed.last_error = "TimeoutError: connection timed out"
        db.add(feed)
        db.commit()
        return 0

    monkeypatch.setattr(
        app.dashboard, "_fetch_inline_for_new_subscription", fake_fetch_fail,
    )

    client = _client()
    _login(client, "alice@example.com")
    r = client.post("/categories", data={"name": "News"})
    cat_id = _extract_id(r.text, "category-")
    r = client.post(
        "/feeds",
        data={"url": "https://broken.example/rss.xml", "category_id": cat_id},
    )
    assert r.status_code == 200
    assert "feed-just-added-chip" in r.text
    assert "feed-just-added-warn" in r.text
    assert "will retry" in r.text
