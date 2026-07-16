"""Category picker for /f/{id}/add — user chooses which of their
existing categories to drop the feed into, or names a new one on
the fly. Server enforces ownership of the chosen category."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select


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


def _seed_feed() -> int:
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://blog.example.com/feed.xml",
            title="Example Blog",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        return feed.id


def test_picker_requires_login():
    feed_id = _seed_feed()
    r = _client().get(f"/f/{feed_id}/add-picker")
    assert r.status_code == 401


def test_picker_lists_only_the_viewers_own_categories():
    feed_id = _seed_feed()
    alice = _client()
    _login(alice, "alice@e.com")
    bob = _client()
    _login(bob, "bob@e.com")

    alice.post("/categories", data={"name": "Weblogs"})
    alice.post("/categories", data={"name": "Zines"})
    bob.post("/categories", data={"name": "Bobs-Private-Stash"})

    r = alice.get(f"/f/{feed_id}/add-picker")
    assert r.status_code == 200
    assert "Weblogs" in r.text
    assert "Zines" in r.text
    assert "Bobs-Private-Stash" not in r.text


def test_picker_still_renders_for_users_with_no_categories():
    """First-time users have no categories yet — the picker still
    works via the 'or new:' field (Discovered is the placeholder)."""
    feed_id = _seed_feed()
    c = _client()
    _login(c, "new@e.com")
    r = c.get(f"/f/{feed_id}/add-picker")
    assert r.status_code == 200
    assert "no categories yet" in r.text
    assert 'name="new_category"' in r.text


def test_add_with_category_id_places_feed_in_that_category():
    feed_id = _seed_feed()
    c = _client()
    _login(c, "alice@e.com")
    resp = c.post("/categories", data={"name": "Longform"})
    # Extract id from the returned partial.
    idx = resp.text.find('id="category-') + len('id="category-')
    end = resp.text.find('"', idx)
    cat_id = int(resp.text[idx:end])

    r = c.post(f"/f/{feed_id}/add", data={"category_id": cat_id})
    assert r.status_code == 200

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        sub = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).one()
    assert sub.category_id == cat_id


def test_add_with_new_category_creates_it_and_uses_it():
    feed_id = _seed_feed()
    c = _client()
    _login(c, "alice@e.com")

    r = c.post(f"/f/{feed_id}/add", data={"new_category": "Weekend Reads"})
    assert r.status_code == 200

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        cats = s.exec(
            select(db_mod.Category).where(db_mod.Category.user_id == alice.id)
        ).all()
        assert [c.name for c in cats] == ["Weekend Reads"]
        sub = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).one()
        assert sub.category_id == cats[0].id


def test_new_category_wins_over_category_id_if_both_provided():
    feed_id = _seed_feed()
    c = _client()
    _login(c, "alice@e.com")
    resp = c.post("/categories", data={"name": "Existing"})
    idx = resp.text.find('id="category-') + len('id="category-')
    cat_id = int(resp.text[idx:resp.text.find('"', idx)])

    c.post(
        f"/f/{feed_id}/add",
        data={"category_id": cat_id, "new_category": "Brand New"},
    )

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        sub = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice.id)
        ).one()
        chosen = s.exec(
            select(db_mod.Category).where(db_mod.Category.id == sub.category_id)
        ).one()
    assert chosen.name == "Brand New"


def test_add_rejects_foreign_category_id():
    feed_id = _seed_feed()
    alice = _client()
    _login(alice, "alice@e.com")
    bob = _client()
    _login(bob, "bob@e.com")
    resp = bob.post("/categories", data={"name": "Bobs-Cat"})
    idx = resp.text.find('id="category-') + len('id="category-')
    bobs_cat_id = int(resp.text[idx:resp.text.find('"', idx)])

    r = alice.post(f"/f/{feed_id}/add", data={"category_id": bobs_cat_id})
    assert r.status_code == 403

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == alice_row.id)
        ).all()
    assert subs == []


def test_add_without_fields_still_falls_back_to_discovered():
    """Old direct-POST callers (before the picker) should keep working —
    an empty POST places the feed in Discovered."""
    feed_id = _seed_feed()
    c = _client()
    _login(c, "alice@e.com")
    r = c.post(f"/f/{feed_id}/add")
    assert r.status_code == 200

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        cats = s.exec(
            select(db_mod.Category).where(db_mod.Category.user_id == alice.id)
        ).all()
    assert [c.name for c in cats] == ["Discovered"]


def test_user_radar_uses_the_same_picker(monkeypatch):
    """"Add to my radar" on /u/{handle} should open the same category
    picker as /f/{id} — no more silent-copy of the owner's category."""
    import app.dashboard

    def fake(url: str) -> dict:
        return {"title": "Proton Blog", "site_url": "https://proton.me",
                "favicon_url": "https://proton.me/favicon.ico"}
    monkeypatch.setattr(app.dashboard, "resolve_feed", fake)

    owner = _client()
    _login(owner, "farid@e.com")
    idx = owner.post("/categories", data={"name": "Weblogs"}).text
    cat_id = int(idx[idx.find('id="category-') + len('id="category-'):].split('"')[0])
    owner.post(
        "/feeds", data={"url": "https://proton.me/blog/feed", "category_id": cat_id}
    )
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        fid = s.exec(
            select(db_mod.Feed).where(db_mod.Feed.url == "https://proton.me/blog/feed")
        ).one().id

    visitor = _client()
    _login(visitor, "bob@e.com")
    page = visitor.get("/u/farid").text
    # Button opens the unified picker (same endpoint as /f/{id}).
    assert f'hx-get="/f/{fid}/add-picker"' in page
    # If bob later subscribes, the row flips to "Following" — no button.
    visitor.post(f"/f/{fid}/add", data={"new_category": "Longform"})
    page2 = visitor.get("/u/farid").text
    assert "✓ Following" in page2
    assert f'hx-get="/f/{fid}/add-picker"' not in page2


def test_new_category_trimmed_and_case_insensitive_reuse():
    """'  Weblogs  ' should reuse an existing 'weblogs' rather than
    creating a duplicate row."""
    feed_id = _seed_feed()
    c = _client()
    _login(c, "alice@e.com")
    c.post("/categories", data={"name": "Weblogs"})

    c.post(f"/f/{feed_id}/add", data={"new_category": "  weblogs  "})

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.handle == "alice")).one()
        cats = s.exec(
            select(db_mod.Category).where(db_mod.Category.user_id == alice.id)
        ).all()
    assert len(cats) == 1
