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
    "berlin-english":            ("en", "Berlin, DE",  4),  # +1 in 0.10.4 (All About Berlin)
    "berlin-kultur":             ("de", "Berlin, DE",  5),
    "better-software-engineer":  ("en", "",           12),
    "better-product-manager":    ("en", "",            7),
    "startup-craft":             ("en", "",            6),
    "tehran-farsi":              ("fa", "Tehran, IR",  4),
    "longform-reads":            ("en", "",           10),
    "engineer-weblogs":          ("en", "",           21),  # new in 0.10.4
    "curious-minds":             ("en", "",            9),  # new in 0.10.4
    "tech-on-youtube":           ("en", "",           12),  # new in 0.10.4
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


def test_seed_syncs_new_feeds_into_existing_collections():
    """0.10.4: the seeder is idempotent PER FEED, not just per
    collection. If a seed's `feeds` list grows in a future release,
    those extra URLs must land on the next boot without wiping any
    existing data. Simulates the All About Berlin addition."""
    import app.db as db_mod
    # Baseline: berlin-english currently has 4 feeds seeded.
    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "berlin-english")).one()
        before = len(s.exec(select(db_mod.CollectionFeed)
                            .where(db_mod.CollectionFeed.collection_id == col.id)).all())
        assert before == 4
        # Delete one CollectionFeed row to simulate a future addition NOT
        # yet on disk. Then re-run the seed and confirm it re-adds it.
        one = s.exec(select(db_mod.CollectionFeed)
                     .where(db_mod.CollectionFeed.collection_id == col.id)
                     .limit(1)).one()
        s.delete(one)
        s.commit()
        after_delete = len(s.exec(select(db_mod.CollectionFeed)
                                  .where(db_mod.CollectionFeed.collection_id == col.id)).all())
        assert after_delete == 3

    # Re-run the seed.
    db_mod._seed_official_collections()

    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "berlin-english")).one()
        after_reseed = len(s.exec(select(db_mod.CollectionFeed)
                                  .where(db_mod.CollectionFeed.collection_id == col.id)).all())
        assert after_reseed == 4  # re-added


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


def test_curator_can_remove_feed_from_collection():
    """0.10.3: curator gets a Remove button on each row; POSTing to it
    drops the CollectionFeed link but leaves the underlying Feed row."""
    import app.db as db_mod
    admin = _client()
    _login(admin, "root@example.com")  # first user = de-facto admin

    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "berlin-nachrichten")).one()
        cf = s.exec(select(db_mod.CollectionFeed)
                    .where(db_mod.CollectionFeed.collection_id == col.id)
                    .order_by(db_mod.CollectionFeed.sort_order)
                    .limit(1)).one()
        feed_id = cf.feed_id
        before_count = len(s.exec(select(db_mod.CollectionFeed)
                                  .where(db_mod.CollectionFeed.collection_id == col.id)).all())

    r = admin.post(f"/collections/berlin-nachrichten/remove/{feed_id}")
    assert r.status_code == 200

    with Session(db_mod.engine) as s:
        after_count = len(s.exec(select(db_mod.CollectionFeed)
                                 .where(db_mod.CollectionFeed.collection_id == col.id)).all())
        assert after_count == before_count - 1
        # Feed row itself must survive.
        feed = s.get(db_mod.Feed, feed_id)
        assert feed is not None


def test_non_curator_cannot_remove_feed():
    import app.db as db_mod
    admin = _client()
    _login(admin, "root@example.com")  # first user
    other = _client()
    _login(other, "u2@example.com")
    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "berlin-nachrichten")).one()
        cf = s.exec(select(db_mod.CollectionFeed)
                    .where(db_mod.CollectionFeed.collection_id == col.id)
                    .limit(1)).one()
        feed_id = cf.feed_id

    r = other.post(f"/collections/berlin-nachrichten/remove/{feed_id}")
    assert r.status_code == 403


def test_curator_sees_remove_button_on_detail_page():
    admin = _client()
    _login(admin, "root@example.com")
    r = admin.get("/collections/berlin-nachrichten")
    assert "collection-feed-remove" in r.text
    assert "hx-post=\"/collections/berlin-nachrichten/remove/" in r.text


def test_non_curator_does_not_see_remove_button():
    admin = _client()
    _login(admin, "root@example.com")  # ensures root exists
    other = _client()
    _login(other, "u2@example.com")
    r = other.get("/collections/berlin-nachrichten")
    assert "collection-feed-remove" not in r.text


def test_suggest_triggers_notification_when_enabled(monkeypatch):
    """0.10.3: POST /suggest fires mail.send_suggestion_notification
    when SUGGESTION_NOTIFY_ENABLED is on. Captures the call rather
    than sending real mail."""
    import app.collections as collections_mod
    import app.config as config_mod

    monkeypatch.setattr(config_mod, "SUGGESTION_NOTIFY_ENABLED", True)
    monkeypatch.setattr(config_mod, "SUGGESTION_NOTIFY_EMAILS", ["admin@example.com"])

    captured: list[dict] = []
    def fake(**kw):
        captured.append(kw)
    monkeypatch.setattr(collections_mod.mail, "send_suggestion_notification", fake)

    c = _client()
    _login(c, "carol@example.com")
    r = c.post("/collections/berlin-nachrichten/suggest",
               data={"feed_url": "https://ex.example/new.rss",
                     "note": "Missing!"})
    assert r.status_code == 200
    assert len(captured) == 1
    kw = captured[0]
    assert kw["collection_slug"] == "berlin-nachrichten"
    assert kw["collection_name"] == "Berlin — Nachrichten"
    assert kw["feed_url"] == "https://ex.example/new.rss"
    assert kw["note"] == "Missing!"
    assert kw["suggester_handle"] == "carol"


def test_notify_disabled_when_config_flag_off(monkeypatch):
    """SUGGESTION_NOTIFY_ENABLED=False is a hard off-switch even if
    emails are configured."""
    import app.config as config_mod
    import app.mail as mail_mod

    monkeypatch.setattr(config_mod, "SUGGESTION_NOTIFY_ENABLED", False)
    monkeypatch.setattr(config_mod, "SUGGESTION_NOTIFY_EMAILS", ["admin@example.com"])

    sent: list[str] = []
    monkeypatch.setattr(mail_mod, "send_email", lambda *a, **k: sent.append("called"))

    mail_mod.send_suggestion_notification(
        collection_slug="x", collection_name="X",
        feed_url="https://x/", note="", suggester_handle="alice",
    )
    assert sent == []


# ─── 0.11.0: collection RSS + language filter ─────────────────────────


def test_collection_rss_returns_valid_rss():
    """/collections/{slug}.rss aggregates newest items across all
    feeds in the collection. Even with zero FeedItems on hand it
    should return a well-formed empty channel, not 500."""
    import xml.etree.ElementTree as ET

    r = _client().get("/collections/berlin-nachrichten.rss")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    assert r.headers.get("cache-control", "").startswith("public")
    # Must parse as valid XML.
    root = ET.fromstring(r.text)
    assert root.tag == "rss"
    channel = root.find("channel")
    assert channel is not None
    assert channel.find("title").text == "Berlin — Nachrichten"
    # Empty is fine here (test-mode has no FeedItems yet).


def test_collection_rss_aggregates_items_from_all_feeds():
    """Populate two Feeds in the collection with items; RSS must
    interleave them newest-first."""
    from datetime import UTC, datetime, timedelta

    import app.db as db_mod

    now = datetime.now(UTC)
    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "berlin-nachrichten")).one()
        cfs = s.exec(select(db_mod.CollectionFeed)
                     .where(db_mod.CollectionFeed.collection_id == col.id)
                     .limit(2)).all()
        feed_a = s.get(db_mod.Feed, cfs[0].feed_id)
        feed_b = s.get(db_mod.Feed, cfs[1].feed_id)
        # Add items with distinct timestamps.
        s.add(db_mod.FeedItem(
            feed_id=feed_a.id, guid="a1", title="From feed A",
            link="https://a.example/1", published_at=now - timedelta(hours=2)))
        s.add(db_mod.FeedItem(
            feed_id=feed_b.id, guid="b1", title="From feed B",
            link="https://b.example/1", published_at=now - timedelta(hours=1)))
        s.commit()

    r = _client().get("/collections/berlin-nachrichten.rss")
    body = r.text
    assert "From feed A" in body
    assert "From feed B" in body
    # B (newer) should appear before A (older).
    assert body.find("From feed B") < body.find("From feed A")


def test_collection_rss_unknown_slug_404():
    r = _client().get("/collections/does-not-exist.rss")
    assert r.status_code == 404


def test_collection_rss_url_appears_on_detail_page():
    r = _client().get("/collections/berlin-nachrichten")
    assert 'href="/collections/berlin-nachrichten.rss"' in r.text
    assert 'data-copy-url="/collections/berlin-nachrichten.rss"' in r.text


def test_language_filter_pill_row_renders_all_langs():
    """The pill row lists every distinct language among public
    collections — regardless of any current filter — so users can
    switch languages without going through 'All'."""
    r = _client().get("/collections")
    body = r.text
    assert "collections-lang-filter" in body
    # Berlin collections declare 'de'; Farsi collection declares 'fa'.
    assert "Deutsch" in body
    assert "فارسی" in body
    assert "English" in body


def test_language_filter_narrows_the_list():
    r = _client().get("/collections?lang=fa")
    body = r.text
    # Only the Farsi collection should appear.
    assert "Tehran" in body
    assert "Berlin — Nachrichten" not in body


def test_language_filter_unknown_lang_shows_helpful_empty_state():
    r = _client().get("/collections?lang=zz")
    body = r.text
    assert "No collections in that language yet" in body


# ─── 0.11.1: admin/curator UI for creating + editing collections ───────


def test_non_admin_cannot_open_new_collection_form():
    a = _client()
    _login(a, "first@example.com")  # de-facto admin
    b = _client()
    _login(b, "second@example.com")
    r = b.get("/collections/new")
    assert r.status_code == 403


def test_admin_creates_collection_then_it_shows_up_in_list():
    import app.db as db_mod
    a = _client()
    _login(a, "first@example.com")
    # +New button visible for admin on /collections
    r = a.get("/collections")
    assert 'href="/collections/new"' in r.text

    r = a.post(
        "/collections/new",
        data={
            "slug": "my-new-list",
            "name": "My New List",
            "description": "hand-picked stuff",
            "language": "en",
            "locale": "",
            "is_official": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/collections/my-new-list")

    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "my-new-list")).one()
        assert col.name == "My New List"
        assert col.is_official is True
        assert col.curator_id is not None  # creator = curator


def test_create_collection_rejects_bad_slug():
    a = _client()
    _login(a, "first@example.com")
    r = a.post(
        "/collections/new",
        data={"slug": "Bad Slug!", "name": "X"},
    )
    assert r.status_code == 400


def test_create_collection_duplicate_slug_returns_409():
    a = _client()
    _login(a, "first@example.com")
    a.post("/collections/new",
           data={"slug": "twice", "name": "First"}, follow_redirects=False)
    r = a.post("/collections/new",
               data={"slug": "twice", "name": "Second"})
    assert r.status_code == 409


def test_curator_can_edit_metadata_but_non_admin_cannot_flip_is_official():
    """Curator of their own (non-official) collection may edit name/desc,
    but toggling is_official is admin-only."""
    import app.db as db_mod
    # Admin creates a non-official collection, curated by user b via
    # ownership transfer through direct DB (simulating handoff).
    a = _client()
    _login(a, "first@example.com")
    a.post("/collections/new",
           data={"slug": "my-list", "name": "My List", "is_official": ""},
           follow_redirects=False)
    b = _client()
    _login(b, "curator-b@example.com")
    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "my-list")).one()
        u = s.exec(select(db_mod.User)
                   .where(db_mod.User.email == "curator-b@example.com")).one()
        col.curator_id = u.id
        s.add(col)
        s.commit()

    # b (curator, not admin) tries to promote to official → stays False.
    r = b.post(
        "/collections/my-list/edit",
        data={"name": "My List Renamed", "description": "d",
              "language": "en", "locale": "", "is_official": "on",
              "is_public": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(db_mod.engine) as s:
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "my-list")).one()
        assert col.name == "My List Renamed"
        assert col.is_official is False  # curator can't self-promote


def test_non_curator_cannot_edit():
    a = _client()
    _login(a, "first@example.com")
    b = _client()
    _login(b, "outsider@example.com")
    r = b.get("/collections/berlin-nachrichten/edit")
    assert r.status_code == 403
    r = b.post("/collections/berlin-nachrichten/edit",
               data={"name": "Hacked"})
    assert r.status_code == 403


def test_curator_add_feed_skips_suggestion_queue(monkeypatch):
    """POST /collections/{slug}/add-feed goes straight into the
    collection — no CollectionSuggestion row created."""
    import app.collections as collections_mod
    import app.db as db_mod
    import app.feeds

    def fake_resolve(url):
        return {"title": "Direct-Add Feed", "site_url": "https://ex.example",
                "favicon_url": "", "is_feed": True}
    monkeypatch.setattr(collections_mod, "resolve_feed", fake_resolve, raising=False)
    monkeypatch.setattr(app.feeds, "resolve_feed", fake_resolve)

    admin = _client()
    _login(admin, "first@example.com")
    r = admin.post(
        "/collections/berlin-nachrichten/add-feed",
        data={"feed_url": "https://ex.example/direct.rss", "note": "curator pick"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with Session(db_mod.engine) as s:
        # No suggestion row created.
        sugs = s.exec(select(db_mod.CollectionSuggestion)).all()
        assert sugs == []
        # Collection feed count grew from 12 to 13.
        col = s.exec(select(db_mod.Collection)
                     .where(db_mod.Collection.slug == "berlin-nachrichten")).one()
        n = len(s.exec(select(db_mod.CollectionFeed)
                       .where(db_mod.CollectionFeed.collection_id == col.id)).all())
        assert n == 13


def test_curator_add_feed_dedup_returns_409(monkeypatch):
    """Adding the same URL twice returns 409 instead of silently duping."""
    import app.collections as collections_mod
    import app.feeds

    def fake_resolve(url):
        return {"title": "Dupe", "site_url": "", "favicon_url": "", "is_feed": True}
    monkeypatch.setattr(collections_mod, "resolve_feed", fake_resolve, raising=False)
    monkeypatch.setattr(app.feeds, "resolve_feed", fake_resolve)

    admin = _client()
    _login(admin, "first@example.com")
    admin.post("/collections/berlin-nachrichten/add-feed",
               data={"feed_url": "https://ex.example/dupe.rss"},
               follow_redirects=False)
    r = admin.post("/collections/berlin-nachrichten/add-feed",
                   data={"feed_url": "https://ex.example/dupe.rss"},
                   follow_redirects=False)
    assert r.status_code == 409


def test_non_curator_cannot_add_feed():
    # First registration is the de-facto admin; establish that before b.
    a = _client()
    _login(a, "first@example.com")
    b = _client()
    _login(b, "outsider@example.com")
    r = b.post("/collections/berlin-nachrichten/add-feed",
               data={"feed_url": "https://ex.example/x.rss"})
    assert r.status_code == 403
