"""Community-curated feed collections (0.10.0).

Cover: seed idempotence, list/detail render, per-feed add, bulk add-all,
suggestion queue, curator moderation, cross-user gate on manage."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select


@pytest.fixture(autouse=True)
def _seed_berlin_collection():
    """This test module operates on the seeded Berlin collection.
    conftest sets RSSFRIENDS_SEED_COLLECTIONS=0 globally so it doesn't
    pollute other tests; here we opt in per test."""
    import app.db as db_mod
    prev = os.environ.get("RSSFRIENDS_SEED_COLLECTIONS")
    os.environ["RSSFRIENDS_SEED_COLLECTIONS"] = "1"
    try:
        db_mod._seed_official_collections()
        yield
    finally:
        if prev is None:
            os.environ["RSSFRIENDS_SEED_COLLECTIONS"] = "0"
        else:
            os.environ["RSSFRIENDS_SEED_COLLECTIONS"] = prev


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


# ─── Seed idempotence ──────────────────────────────────────────────────


EXPECTED_SEEDS = {
    # slug: (language, locale, feed_count)
    "berlin-nachrichten":        ("de", "Berlin, DE", 12),
    "berlin-english":            ("en", "Berlin, DE",  3),
    "berlin-kultur":             ("de", "Berlin, DE",  5),
    "better-software-engineer":  ("en", "",           12),
    "better-product-manager":    ("en", "",            7),
    "startup-craft":             ("en", "",            6),
    "tehran-farsi":              ("fa", "Tehran, IR",  4),
    "longform-reads":            ("en", "",           10),
}


def test_all_official_collections_seeded_on_init():
    """`init_db()` runs on the fresh-schema fixture. Every seed
    collection should exist with its declared feed count."""
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        for slug, (lang, locale, feed_count) in EXPECTED_SEEDS.items():
            col = s.exec(
                select(db_mod.Collection).where(db_mod.Collection.slug == slug)
            ).one()
            assert col.is_official is True
            assert col.language == lang
            assert col.locale == locale
            rows = s.exec(
                select(db_mod.CollectionFeed)
                .where(db_mod.CollectionFeed.collection_id == col.id)
            ).all()
            assert len(rows) == feed_count, (
                f"{slug}: expected {feed_count} feeds, got {len(rows)}"
            )


def test_re_running_init_db_is_idempotent():
    """Second call to init_db() shouldn't duplicate anything."""
    import app.db as db_mod
    db_mod.init_db()
    with Session(db_mod.engine) as s:
        for slug in EXPECTED_SEEDS:
            cols = s.exec(
                select(db_mod.Collection).where(db_mod.Collection.slug == slug)
            ).all()
            assert len(cols) == 1, f"{slug}: expected 1 row, got {len(cols)}"


def test_berlin_spectator_feed_is_dual_listed():
    """Berlin Spectator appears in both Nachrichten and English collections
    but shares one Feed row (dedup by URL)."""
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = s.exec(
            select(db_mod.Feed).where(db_mod.Feed.url == "https://berlinspectator.com/feed/")
        ).one()
        # Should be linked from two CollectionFeed rows.
        links = s.exec(
            select(db_mod.CollectionFeed)
            .where(db_mod.CollectionFeed.feed_id == feed.id)
        ).all()
        assert len(links) == 2


# ─── Read routes ───────────────────────────────────────────────────────


def test_list_collections_renders():
    r = _client().get("/collections")
    assert r.status_code == 200
    assert "Berlin — Nachrichten" in r.text
    assert "collection-card" in r.text


def test_collection_detail_renders_with_feeds():
    r = _client().get("/collections/berlin-nachrichten")
    assert r.status_code == 200
    body = r.text
    assert "Berlin — Nachrichten" in body
    assert "rbb24" in body
    assert "Tagesspiegel" in body
    assert "<strong>12</strong> feed" in body


def test_unknown_collection_returns_404():
    r = _client().get("/collections/nope-nada")
    assert r.status_code == 404


def test_anonymous_visitor_sees_signin_cta_not_add():
    """Anon users see 'Sign in' pills, not '+ Add' buttons."""
    r = _client().get("/collections/berlin-nachrichten")
    assert "Sign in to add these to your radar" in r.text
    # No POST button when logged out.
    assert "hx-post=\"/collections/berlin-nachrichten/add/" not in r.text


# ─── Per-feed add ──────────────────────────────────────────────────────


def test_add_one_feed_creates_subscription_under_named_category():
    import app.db as db_mod
    c = _client()
    _login(c, "alice@example.com")
    # Pick the first feed_id in the seeded collection.
    with Session(db_mod.engine) as s:
        cf = s.exec(select(db_mod.CollectionFeed)
                    .order_by(db_mod.CollectionFeed.sort_order)
                    .limit(1)).one()
        feed_id = cf.feed_id
    r = c.post(f"/collections/berlin-nachrichten/add/{feed_id}")
    assert r.status_code == 200
    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User)
                       .where(db_mod.User.handle == "alice")).one()
        sub = s.exec(select(db_mod.Subscription)
                     .where(db_mod.Subscription.user_id == alice.id)).one()
        assert sub.feed_id == feed_id
        cat = s.get(db_mod.Category, sub.category_id)
        assert cat.name == "Berlin — Nachrichten"


def test_add_one_feed_not_in_collection_404():
    """Prevents anyone from POSTing arbitrary feed_ids under a public
    collection URL to shove random subs onto their radar via the
    collection-add code path. Not a real security issue (they can
    subscribe normally) but the endpoint must be tight."""
    import app.db as db_mod
    c = _client()
    _login(c, "alice@example.com")
    with Session(db_mod.engine) as s:
        # Create a Feed NOT in the collection.
        rogue = db_mod.Feed(url="https://rogue.example/feed", title="Rogue")
        s.add(rogue)
        s.commit()
        s.refresh(rogue)
        rogue_id = rogue.id
    r = c.post(f"/collections/berlin-nachrichten/add/{rogue_id}")
    assert r.status_code == 404


# ─── Bulk add-all ──────────────────────────────────────────────────────


def test_add_all_bulk_subscribes_and_reports_added_count():
    import app.db as db_mod
    c = _client()
    _login(c, "bob@example.com")
    r = c.post("/collections/berlin-nachrichten/add-all")
    assert r.status_code == 200
    assert "Added 12 feeds" in r.text
    with Session(db_mod.engine) as s:
        bob = s.exec(select(db_mod.User)
                     .where(db_mod.User.handle == "bob")).one()
        subs = s.exec(select(db_mod.Subscription)
                      .where(db_mod.Subscription.user_id == bob.id)).all()
        assert len(subs) == 12


def test_add_all_idempotent_reports_already_count():
    c = _client()
    _login(c, "alice@example.com")
    c.post("/collections/berlin-nachrichten/add-all")
    r = c.post("/collections/berlin-nachrichten/add-all")
    assert r.status_code == 200
    assert "Added 0 feeds" in r.text
    assert "12 were already there" in r.text


# ─── Suggestions + moderation ──────────────────────────────────────────


def test_user_can_suggest_a_feed():
    import app.db as db_mod
    c = _client()
    _login(c, "carol@example.com")
    r = c.post(
        "/collections/berlin-nachrichten/suggest",
        data={"feed_url": "https://example.com/new-berlin-feed.rss",
              "note": "Missing this one!"},
    )
    assert r.status_code == 200
    assert "queued for" in r.text  # copy on the thank-you card
    with Session(db_mod.engine) as s:
        sug = s.exec(select(db_mod.CollectionSuggestion)).one()
        assert sug.status == "pending"
        assert sug.feed_url == "https://example.com/new-berlin-feed.rss"


def test_suggest_dup_from_same_user_409():
    c = _client()
    _login(c, "carol@example.com")
    c.post("/collections/berlin-nachrichten/suggest",
           data={"feed_url": "https://example.com/dup.rss"})
    r = c.post("/collections/berlin-nachrichten/suggest",
               data={"feed_url": "https://example.com/dup.rss"})
    assert r.status_code == 409


def test_suggest_rejects_bad_url_400():
    c = _client()
    _login(c, "carol@example.com")
    r = c.post("/collections/berlin-nachrichten/suggest",
               data={"feed_url": "not-a-url"})
    assert r.status_code == 400


def test_non_curator_cannot_moderate():
    """First-registered user in the fresh-schema fixture is the
    de-facto admin (curator_id is NULL because 'ahmadian-farid-1988'
    doesn't exist in tests). Only that first user can manage the
    system-curated Berlin collection."""
    a = _client()
    _login(a, "first@example.com")  # first user → de-facto admin
    b = _client()
    _login(b, "second@example.com")

    # first can manage
    r = a.get("/collections/berlin-nachrichten/manage")
    assert r.status_code == 200
    # second cannot
    r = b.get("/collections/berlin-nachrichten/manage")
    assert r.status_code == 403


def test_curator_accepts_suggestion_and_feed_is_added(monkeypatch):
    """Accepting a suggestion runs it through the same feeds pipeline
    (normalize + resolve) as any manual subscribe. Stub the resolver
    to avoid network."""
    import app.collections as collections_mod
    import app.db as db_mod

    def fake_resolve(url):
        return {"title": "Suggested Feed", "site_url": "https://ex.example",
                "favicon_url": "", "is_feed": True}
    monkeypatch.setattr(collections_mod, "resolve_feed", fake_resolve, raising=False)
    # ^ resolve_feed is imported inside the function so this monkeypatch
    # only takes if we route through the module attr. Fall back:
    import app.feeds
    monkeypatch.setattr(app.feeds, "resolve_feed", fake_resolve)

    admin = _client()
    _login(admin, "root@example.com")
    other = _client()
    _login(other, "u2@example.com")

    other.post(
        "/collections/berlin-nachrichten/suggest",
        data={"feed_url": "https://ex.example/suggested-feed.rss"},
    )
    with Session(db_mod.engine) as s:
        sug = s.exec(select(db_mod.CollectionSuggestion)
                     .where(db_mod.CollectionSuggestion.status == "pending")).one()
    r = admin.post(
        f"/collections/berlin-nachrichten/suggestions/{sug.id}/accept",
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(db_mod.engine) as s:
        s2 = s.get(db_mod.CollectionSuggestion, sug.id)
        assert s2.status == "accepted"
        assert s2.reviewed_at is not None
        # Collection feed count grew from 12 to 13.
        n = len(s.exec(select(db_mod.CollectionFeed)
                       .where(db_mod.CollectionFeed.collection_id ==
                              s.exec(select(db_mod.Collection).where(
                                  db_mod.Collection.slug == "berlin-nachrichten"
                              )).one().id)).all())
        assert n == 13


def test_curator_rejects_suggestion():
    import app.db as db_mod
    admin = _client()
    _login(admin, "root@example.com")
    other = _client()
    _login(other, "u2@example.com")
    other.post("/collections/berlin-nachrichten/suggest",
               data={"feed_url": "https://spam.example/no.rss"})
    with Session(db_mod.engine) as s:
        sug = s.exec(select(db_mod.CollectionSuggestion)).one()
    r = admin.post(
        f"/collections/berlin-nachrichten/suggestions/{sug.id}/reject",
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(db_mod.engine) as s:
        s2 = s.get(db_mod.CollectionSuggestion, sug.id)
        assert s2.status == "rejected"


# ─── Discover integration ──────────────────────────────────────────────


def test_discover_page_shows_featured_collection_card():
    r = _client().get("/discover")
    assert r.status_code == 200
    body = r.text
    assert "Browse collections" in body
    assert "Berlin — Nachrichten" in body
    assert "/collections/berlin-nachrichten" in body
