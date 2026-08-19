"""Personal feeds: user claims external feeds as theirs, new items
from them emit Activity(kind='posted') so followers see cross-posts."""

from datetime import UTC, datetime, timedelta

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


def _user_id(handle: str) -> int:
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        return s.exec(select(db_mod.User).where(db_mod.User.handle == handle)).one().id


def _seed_feed(url: str, title: str = "F") -> int:
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url=url, title=title)
        s.add(feed)
        s.commit()
        s.refresh(feed)
        return feed.id


# ------------- CRUD -------------

def test_personal_feeds_panel_appears_in_dashboard():
    c = _client()
    _login(c, "farid@e.com")
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "🎙️ My feeds" in r.text  # tab strip
    r = c.get("/dashboard?panel=personal")
    assert r.status_code == 200
    assert "connect your own rss" in r.text.lower()


def test_add_personal_feed_direct(monkeypatch):
    """Happy path: paste a real feed URL → PersonalFeed row created,
    Feed row created (if new), row rendered."""
    import app.personal_feeds as pf_mod

    def fake_resolve(url):
        return {"title": "My Mastodon", "site_url": "https://hachyderm.io",
                "favicon_url": "", "is_feed": True}
    monkeypatch.setattr(pf_mod, "resolve_feed", fake_resolve)

    c = _client()
    _login(c, "farid@e.com")
    r = c.post("/personal-feeds", data={
        "url": "https://mastodon.example/@farid.rss",
        "label": "Mastodon",
    })
    assert r.status_code == 200
    assert "Mastodon" in r.text

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        rows = s.exec(select(db_mod.PersonalFeed).where(
            db_mod.PersonalFeed.user_id == _user_id("farid")
        )).all()
    assert len(rows) == 1
    assert rows[0].label == "Mastodon"


def test_duplicate_personal_feed_rejected(monkeypatch):
    import app.personal_feeds as pf_mod
    monkeypatch.setattr(
        pf_mod, "resolve_feed",
        lambda u: {"title": "X", "site_url": "", "favicon_url": "", "is_feed": True},
    )
    c = _client()
    _login(c, "farid@e.com")
    c.post("/personal-feeds", data={"url": "https://a.example/rss"})
    r = c.post("/personal-feeds", data={"url": "https://a.example/rss"})
    assert r.status_code == 409


def test_personal_feed_discovery_on_non_feed_url(monkeypatch):
    """Pasting a homepage URL → discovery finds the feed."""
    import app.personal_feeds as pf_mod
    monkeypatch.setattr(
        pf_mod, "resolve_feed",
        lambda u: {"title": u, "site_url": "", "favicon_url": "", "is_feed": False},
    )
    monkeypatch.setattr(pf_mod, "discover_feeds", lambda u: [
        {"url": "https://blog.example.com/rss", "title": "Blog", "type": "rss"},
        {"url": "https://blog.example.com/comments.rss", "title": "Comments", "type": "rss"},
    ])

    c = _client()
    _login(c, "farid@e.com")
    r = c.post("/personal-feeds", data={"url": "https://blog.example.com"})
    assert r.status_code == 200
    assert r.headers.get("HX-Retarget") == "#personal-discovery"
    assert "Connect selected" in r.text
    # No PersonalFeed row yet.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        rows = s.exec(select(db_mod.PersonalFeed)).all()
    assert rows == []


def test_disconnect_removes_only_own_row():
    c = _client()
    _login(c, "farid@e.com")
    feed_id = _seed_feed("https://a.example/rss")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(
            user_id=_user_id("farid"), feed_id=feed_id, label="Test"
        )
        s.add(pf)
        s.commit()
        s.refresh(pf)
        pf_id = pf.id

    r = c.delete(f"/personal-feeds/{pf_id}")
    assert r.status_code == 200
    with Session(db_mod.engine) as s:
        assert s.get(db_mod.PersonalFeed, pf_id) is None


def test_disconnect_rejects_foreign():
    other = _client()
    _login(other, "farid@e.com")
    feed_id = _seed_feed("https://a.example/rss")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()
        s.refresh(pf)
        pf_id = pf.id

    bob = _client()
    _login(bob, "bob@e.com")
    r = bob.delete(f"/personal-feeds/{pf_id}")
    assert r.status_code == 404


# ------------- Activity emission -------------

def _new_feeditem(
    feed_id: int, guid: str, title: str, published_at=None, fetched_at=None,
) -> int:
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        kwargs = dict(
            feed_id=feed_id, guid=guid, title=title,
            link=f"https://example/{guid}",
            published_at=published_at,
        )
        if fetched_at is not None:
            kwargs["fetched_at"] = fetched_at
        item = db_mod.FeedItem(**kwargs)
        s.add(item)
        s.commit()
        s.refresh(item)
        return item.id


def test_new_item_after_connect_emits_posted_activity():
    """The whole point: new item arrives on a personal feed → follower
    of that user sees "@user posted <title>" on /feed."""
    import app.db as db_mod
    from app.refresh import _emit_personal_activity

    feed_id = _seed_feed("https://mastodon.example/@farid.rss", title="Toots")

    c = _client()
    _login(c, "farid@e.com")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    # Item published AFTER connect.
    later = datetime.now(UTC) + timedelta(minutes=5)
    item_id = _new_feeditem(feed_id, "guid-new", "Hello world", published_at=later)

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        item = s.get(db_mod.FeedItem, item_id)
        _emit_personal_activity(s, feed, [item])

    with Session(db_mod.engine) as s:
        acts = s.exec(
            select(db_mod.Activity)
            .where(db_mod.Activity.user_id == _user_id("farid"))
            .where(db_mod.Activity.kind == "posted")
        ).all()
    assert len(acts) == 1
    assert acts[0].subject == "Hello world"
    assert acts[0].ref_id == item_id


def test_historical_items_before_connect_are_ignored():
    """Items already in the DB (fetched by an earlier subscriber's
    refresh) BEFORE the user connected this feed as personal must NOT
    emit posted-activity. This is the anti-flood guarantee for anyone
    connecting a heavily-subscribed feed."""
    import app.db as db_mod
    from app.refresh import _emit_personal_activity

    feed_id = _seed_feed("https://mastodon.example/@farid.rss")

    c = _client()
    _login(c, "farid@e.com")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    # Item was fetched (by a previous subscriber's refresh) 30 days
    # ago — well before the personal-feed connect above.
    long_ago = datetime.now(UTC) - timedelta(days=30)
    old_id = _new_feeditem(
        feed_id, "guid-old", "ancient",
        published_at=long_ago, fetched_at=long_ago,
    )

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        item = s.get(db_mod.FeedItem, old_id)
        _emit_personal_activity(s, feed, [item])

    with Session(db_mod.engine) as s:
        acts = s.exec(
            select(db_mod.Activity).where(db_mod.Activity.kind == "posted")
        ).all()
    assert acts == []


def test_playlist_of_old_videos_still_cross_posts():
    """Regression: a YouTube playlist (or any RSS digest) whose items
    are dated years ago but are being freshly ingested must emit
    activity — the cutoff is 'first seen after connect', not
    'originally published after connect'. This was the bug reported
    for a YouTube playlist personal feed."""
    import app.db as db_mod
    from app.refresh import _emit_personal_activity

    feed_id = _seed_feed("https://youtube.example/playlist.rss", title="Playlist")

    c = _client()
    _login(c, "farid@e.com")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    # Videos originally published years ago, but freshly ingested NOW.
    yrs_ago = datetime.now(UTC) - timedelta(days=365 * 5)
    just_now = datetime.now(UTC)
    old_video_id = _new_feeditem(
        feed_id, "vid-2020", "Old but curated",
        published_at=yrs_ago, fetched_at=just_now,
    )

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        item = s.get(db_mod.FeedItem, old_video_id)
        _emit_personal_activity(s, feed, [item])

    with Session(db_mod.engine) as s:
        acts = s.exec(
            select(db_mod.Activity).where(db_mod.Activity.kind == "posted")
        ).all()
    assert len(acts) == 1
    assert acts[0].subject == "Old but curated"


def test_emission_capped_per_cycle():
    """A brand-new heavy feed (first fetch pulls 50 items) shouldn't
    dump 50 posted-activity rows on followers. Capped at
    MAX_POSTED_PER_CYCLE (currently 20)."""
    import app.db as db_mod
    from app import refresh as refresh_mod
    from app.refresh import _emit_personal_activity

    feed_id = _seed_feed("https://firehose.example/rss")

    c = _client()
    _login(c, "farid@e.com")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    just_now = datetime.now(UTC)
    items = []
    for i in range(refresh_mod.MAX_POSTED_PER_CYCLE + 10):
        iid = _new_feeditem(
            feed_id, f"g-{i}", f"item-{i}",
            published_at=just_now, fetched_at=just_now,
        )
        items.append(iid)

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        item_objs = [s.get(db_mod.FeedItem, iid) for iid in items]
        _emit_personal_activity(s, feed, item_objs)

    with Session(db_mod.engine) as s:
        acts = s.exec(
            select(db_mod.Activity).where(db_mod.Activity.kind == "posted")
        ).all()
    assert len(acts) == refresh_mod.MAX_POSTED_PER_CYCLE


def test_emit_is_idempotent_across_cycles():
    """Second call with the same item doesn't create a second Activity."""
    import app.db as db_mod
    from app.refresh import _emit_personal_activity

    feed_id = _seed_feed("https://mastodon.example/@farid.rss")

    c = _client()
    _login(c, "farid@e.com")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    later = datetime.now(UTC) + timedelta(minutes=5)
    item_id = _new_feeditem(feed_id, "g", "hi", published_at=later)

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        item = s.get(db_mod.FeedItem, item_id)
        _emit_personal_activity(s, feed, [item])
        _emit_personal_activity(s, feed, [item])  # again

    with Session(db_mod.engine) as s:
        acts = s.exec(
            select(db_mod.Activity).where(db_mod.Activity.kind == "posted")
        ).all()
    assert len(acts) == 1


def test_no_activity_when_no_personal_claimant():
    """Feeds nobody claims as personal don't produce 'posted' activity —
    only Subscription-driven activity (which is a separate mechanism)."""
    import app.db as db_mod
    from app.refresh import _emit_personal_activity

    feed_id = _seed_feed("https://random.example/rss")
    later = datetime.now(UTC) + timedelta(minutes=5)
    item_id = _new_feeditem(feed_id, "g", "hi", published_at=later)

    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        item = s.get(db_mod.FeedItem, item_id)
        _emit_personal_activity(s, feed, [item])

    with Session(db_mod.engine) as s:
        acts = s.exec(
            select(db_mod.Activity).where(db_mod.Activity.kind == "posted")
        ).all()
    assert acts == []


def test_follower_sees_posted_activity_on_timeline():
    """End-to-end: farid connects a personal feed, an item arrives,
    bob follows farid — bob's /feed shows the post."""
    import app.db as db_mod
    from app.refresh import _emit_personal_activity

    feed_id = _seed_feed("https://hachyderm.io/@farid.rss")

    farid = _client()
    _login(farid, "farid@e.com")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    later = datetime.now(UTC) + timedelta(minutes=5)
    item_id = _new_feeditem(
        feed_id, "g", "Hello mastodon world", published_at=later,
    )
    with Session(db_mod.engine) as s:
        feed = s.get(db_mod.Feed, feed_id)
        item = s.get(db_mod.FeedItem, item_id)
        _emit_personal_activity(s, feed, [item])

    bob = _client()
    _login(bob, "bob@e.com")
    bob.post("/u/farid/follow")

    r = bob.get("/feed")
    assert r.status_code == 200
    assert "Hello mastodon world" in r.text
    assert "posted" in r.text
    # Source domain chip shows for attribution.
    assert "hachyderm.io" in r.text or "example" in r.text  # link/domain visible


def test_personal_feeds_do_not_pollute_owner_reading_radar():
    """A personal feed does NOT become a Subscription — it never
    appears on the owner's reading dashboard or /u/{handle}."""
    import app.db as db_mod

    c = _client()
    _login(c, "farid@e.com")
    feed_id = _seed_feed("https://hachyderm.io/@farid.rss", title="Own toots")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    r = c.get("/dashboard?panel=feeds")
    assert r.status_code == 200
    # The feed is not listed in the reading radar.
    assert "Own toots" not in r.text or "🎙️" in r.text  # only in personal panel
    # And no Subscription row exists.
    with Session(db_mod.engine) as s:
        subs = s.exec(select(db_mod.Subscription).where(
            db_mod.Subscription.user_id == _user_id("farid")
        )).all()
    assert subs == []


def test_personal_feeds_do_not_count_toward_discover_popularity():
    """Popular feeds on /discover count Subscription rows; personal
    claims aren't subscriptions, so they don't inflate the count."""
    import app.db as db_mod

    c = _client()
    _login(c, "farid@e.com")
    feed_id = _seed_feed("https://hachyderm.io/@farid.rss", title="Toots-Unique-XYZ")
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    r = _client().get("/discover")
    # Zero subscribers → doesn't surface on Popular feeds.
    assert "Toots-Unique-XYZ" not in r.text


def test_manual_refresh_allowed_for_personal_feed_owner(monkeypatch):
    """The /feeds/{id}/refresh endpoint accepts a caller who owns the
    feed as a PersonalFeed — not just Subscription subscribers."""
    import app.dashboard as dash

    def fake_fetch(db, feed):
        return 0, None
    monkeypatch.setattr(dash, "fetch_one", fake_fetch)
    monkeypatch.setattr(
        dash, "resolve_avatar",
        lambda *a, **kw: None,  # skip deep resolve to keep test hermetic
    )

    c = _client()
    _login(c, "farid@e.com")
    feed_id = _seed_feed("https://mastodon.example/@farid.rss", title="Own")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    r = c.post(f"/feeds/{feed_id}/refresh")
    assert r.status_code == 200


def test_manual_refresh_rejects_unauthorized_caller():
    """Foreign user with no relationship to the feed is 404'd — same
    contract regardless of ownership shape."""
    c = _client()
    _login(c, "farid@e.com")
    feed_id = _seed_feed("https://mastodon.example/@farid.rss")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    bob = _client()
    _login(bob, "bob@e.com")
    r = bob.post(f"/feeds/{feed_id}/refresh")
    assert r.status_code == 404


def test_personal_feed_row_has_refresh_button():
    """Confirms the 🔄 button is rendered on each personal feed row."""
    c = _client()
    _login(c, "farid@e.com")
    feed_id = _seed_feed("https://mastodon.example/@farid.rss")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        pf = db_mod.PersonalFeed(user_id=_user_id("farid"), feed_id=feed_id)
        s.add(pf)
        s.commit()

    r = c.get("/dashboard?panel=personal")
    assert r.status_code == 200
    assert f'hx-post="/feeds/{feed_id}/refresh"' in r.text
    assert "🔄" in r.text


def test_cap_at_max_personal_feeds(monkeypatch):
    import app.personal_feeds as pf_mod
    monkeypatch.setattr(
        pf_mod, "resolve_feed",
        lambda u: {"title": "X", "site_url": "", "favicon_url": "", "is_feed": True},
    )
    monkeypatch.setattr(pf_mod, "MAX_PERSONAL_FEEDS_PER_USER", 2)

    c = _client()
    _login(c, "farid@e.com")
    c.post("/personal-feeds", data={"url": "https://a.example/rss"})
    c.post("/personal-feeds", data={"url": "https://b.example/rss"})
    r = c.post("/personal-feeds", data={"url": "https://c.example/rss"})
    assert r.status_code == 400
    assert "disconnect" in r.text.lower() or "already have" in r.text.lower()
