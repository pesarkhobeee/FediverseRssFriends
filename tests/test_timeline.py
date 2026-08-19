"""Home timeline at /feed: auth gate, followee activity + own items,
interleave order, empty state, root redirect."""

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


def test_feed_requires_login():
    r = _client().get("/feed")
    assert r.status_code == 401


def test_root_redirects_signed_in_to_own_page():
    client = _client()
    _login(client, "u@e.com")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    # Signed-in root goes to the user's own public page (handle derived
    # from email local-part).
    assert r.headers["location"] == "/u/u"


def test_root_anonymous_still_lands_on_signup():
    r = _client().get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Send magic link" in r.text


def test_empty_feed_has_helpful_hint():
    client = _client()
    _login(client, "u@e.com")
    r = client.get("/feed")
    assert r.status_code == 200
    assert "Your Friends page is quiet" in r.text


def test_feed_shows_followee_activity():
    """When A follows B and B stars an item, A sees the star event on /feed."""
    import app.db as db_mod

    # Seed B with a feed + item, and a favorite.
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Example")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="urn:1", title="Interesting post",
            link="https://x.example/1",
            published_at=datetime.now(UTC) - timedelta(hours=2),
        )
        s.add(item)
        s.commit()
        s.refresh(item)

    b = _client()
    _login(b, "b@e.com")
    b.post(f"/items/{item.id}/bookmark")

    a = _client()
    _login(a, "a@e.com")
    a.post("/u/b/follow")

    r = a.get("/feed")
    assert r.status_code == 200
    assert "@b" in r.text
    assert "bookmarked" in r.text
    assert "Interesting post" in r.text


def test_feed_does_not_show_own_subscription_items():
    """Regression: the timeline is a social feed of followees' activity,
    NOT a merged reader for your own subscriptions. Fresh items from
    your subs live on /u/{handle} and /dashboard — not here."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="My Blog")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.FeedItem(
            feed_id=feed.id, guid="p1", title="Fresh post",
            link="https://x.example/1",
            published_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        s.commit()

    client = _client()
    _login(client, "u@e.com")
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "u")).one()
        cat = db_mod.Category(user_id=user.id, name="Blogs", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        feed = s.exec(select(db_mod.Feed).where(db_mod.Feed.url == "https://x.example/feed")).one()
        s.add(db_mod.Subscription(
            user_id=user.id, feed_id=feed.id, category_id=cat.id, sort_order=1
        ))
        s.commit()

    r = client.get("/feed")
    assert r.status_code == 200
    # With no follows and no external activity, the timeline is empty
    # regardless of how many items the viewer's own feeds have.
    assert "Fresh post" not in r.text
    assert "Your Friends page is quiet" in r.text


def test_star_events_show_up_for_followers():
    """The social path: when @b stars an item and @a follows @b,
    @a's timeline shows "@b starred X" — the star is public."""
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Blog")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="p1", title="Great post",
            link="https://x.example/1",
            published_at=datetime.now(UTC) - timedelta(hours=1),
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        item_id = item.id

    b = _client()
    _login(b, "b@e.com")
    b.post(f"/items/{item_id}/bookmark")

    a = _client()
    _login(a, "a@e.com")
    a.post("/u/b/follow")

    r = a.get("/feed")
    assert "@b" in r.text
    assert "bookmarked" in r.text
    assert "Great post" in r.text


def test_feed_shows_add_to_radar_pill_for_unfollowed_feed():
    """/feed → 'subscribed' activity from a followee whose feed the
    viewer does NOT yet have should render an "Add to my radar" pill
    (via _feed_cta.html). Reused from /discover, so we grep for the
    same signature ('Add to my radar' text)."""
    from sqlmodel import Session, select

    import app.db as db_mod

    # b subscribes to a feed; a follows b.
    a = _client()
    _login(a, "a@e.com")
    b = _client()
    _login(b, "b@e.com")
    b.post("/categories", data={"name": "News"})

    with Session(db_mod.engine) as s:
        b_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "b")).one()
        cat = s.exec(select(db_mod.Category).where(db_mod.Category.user_id == b_row.id)).one()
        feed = db_mod.Feed(url="https://ex.example/feed", title="Example")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.Subscription(
            user_id=b_row.id, feed_id=feed.id, category_id=cat.id, sort_order=1,
        ))
        import app.activity as act
        act.record(
            s, b_row, "subscribed", feed.title,
            subject_url=feed.site_url or feed.url,
            ref_type="feed", ref_id=feed.id,
        )
        s.commit()

    a.post("/u/b/follow")
    r = a.get("/feed")
    assert r.status_code == 200
    assert "Add to my radar" in r.text
    assert 'class="timeline-cta"' in r.text


def test_feed_hides_add_pill_when_viewer_already_subscribed():
    """Same setup but the viewer ALSO subscribes to the same feed —
    the pill should not render because they already have it."""
    from sqlmodel import Session, select

    import app.db as db_mod

    a = _client()
    _login(a, "a@e.com")
    b = _client()
    _login(b, "b@e.com")

    # a and b each have a "News" category; both subscribe to the same feed.
    r = a.post("/categories", data={"name": "News"})
    r = b.post("/categories", data={"name": "News"})

    with Session(db_mod.engine) as s:
        a_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "a")).one()
        b_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "b")).one()
        a_cat = s.exec(select(db_mod.Category).where(db_mod.Category.user_id == a_row.id)).one()
        b_cat = s.exec(select(db_mod.Category).where(db_mod.Category.user_id == b_row.id)).one()
        feed = db_mod.Feed(url="https://ex.example/feed", title="Example")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.Subscription(
            user_id=a_row.id, feed_id=feed.id, category_id=a_cat.id, sort_order=1,
        ))
        s.add(db_mod.Subscription(
            user_id=b_row.id, feed_id=feed.id, category_id=b_cat.id, sort_order=1,
        ))
        import app.activity as act
        act.record(
            s, b_row, "subscribed", feed.title,
            subject_url=feed.site_url or feed.url,
            ref_type="feed", ref_id=feed.id,
        )
        s.commit()

    a.post("/u/b/follow")
    r = a.get("/feed")
    assert r.status_code == 200
    # b's activity row is present but pill is not (viewer already subs).
    assert "@b" in r.text
    assert "Add to my radar" not in r.text


def _seed_remote_note_for(email: str):
    """Create a RemoteActor + RemoteFollowing(accepted) + RemoteItem so
    the given user's timeline includes one fediverse note."""
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        u = s.exec(select(db_mod.User).where(db_mod.User.email == email)).one()
        ra = db_mod.RemoteActor(
            actor_url="https://mastodon.example/users/simon",
            inbox_url="https://mastodon.example/users/simon/inbox",
            public_key_pem="-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----",
            preferred_username="simon",
            display_name="Simon",
        )
        s.add(ra)
        s.commit()
        s.refresh(ra)
        s.add(db_mod.RemoteFollowing(
            user_id=u.id, remote_actor_id=ra.id,
            follow_activity_id="https://local.test/activities/1",
            accepted_at=datetime.now(UTC),
        ))
        s.add(db_mod.RemoteItem(
            remote_actor_id=ra.id,
            ap_note_id="https://mastodon.example/notes/1",
            content_html="<p>hello from the fediverse</p>",
            note_url="https://mastodon.example/notes/1",
            published_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        s.commit()


def test_feed_no_source_chips_without_fediverse_items():
    """A user with only local activity shouldn't see the chip row —
    it would just be visual clutter."""
    a = _client()
    _login(a, "a@e.com")
    r = a.get("/feed")
    assert r.status_code == 200
    assert 'class="feed-src-filter"' not in r.text


def test_feed_source_chips_render_when_fediverse_present():
    a = _client()
    _login(a, "a@e.com")
    _seed_remote_note_for("a@e.com")
    r = a.get("/feed")
    assert r.status_code == 200
    assert 'class="feed-src-filter"' in r.text
    assert "hello from the fediverse" in r.text


def test_feed_src_local_hides_remote_notes():
    a = _client()
    _login(a, "a@e.com")
    _seed_remote_note_for("a@e.com")
    r = a.get("/feed?src=local")
    assert r.status_code == 200
    assert "hello from the fediverse" not in r.text
    # Chip row still rendered, and "local" is the active tab.
    assert "feed-src-chip-active" in r.text


def test_feed_src_fediverse_shows_only_remote_notes():
    a = _client()
    _login(a, "a@e.com")
    _seed_remote_note_for("a@e.com")
    r = a.get("/feed?src=fediverse")
    assert r.status_code == 200
    assert "hello from the fediverse" in r.text


def _seed_bookmarks_for_followee(follower_email: str, followee_email: str, n: int):
    """Have follower→follow followee, then create n bookmark events on
    followee with descending timestamps. Returns the list of item ids
    in newest-first order."""
    import app.activity as act
    import app.db as db_mod

    fol = _client()
    _login(fol, followee_email)
    a = _client()
    _login(a, follower_email)
    a.post(f"/u/{followee_email.split('@')[0]}/follow")

    item_ids = []
    with Session(db_mod.engine) as s:
        followee = s.exec(
            select(db_mod.User).where(db_mod.User.email == followee_email)
        ).one()
        feed = db_mod.Feed(url=f"https://ex.example/{followee_email}", title="Ex")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        now = datetime.now(UTC)
        for i in range(n):
            item = db_mod.FeedItem(
                feed_id=feed.id, guid=f"urn:{i}", title=f"Item {i}",
                link=f"https://ex.example/{i}",
                published_at=now - timedelta(minutes=i),
            )
            s.add(item)
            s.commit()
            s.refresh(item)
            item_ids.append(item.id)
            # Backdate the activity row to ensure a strict order.
            act.record(
                s, followee, "bookmarked", item.title,
                subject_url=item.link, ref_type="item", ref_id=item.id,
            )
            s.commit()
            ev = s.exec(
                select(db_mod.Activity)
                .where(db_mod.Activity.user_id == followee.id)
                .where(db_mod.Activity.ref_id == item.id)
            ).one()
            ev.created_at = now - timedelta(minutes=i)
            s.add(ev)
            s.commit()
    return a, item_ids


def test_feed_paginates_with_load_more_link():
    """With more than one page of activity, the first page renders a
    cursor-based 'Load more' button that preserves paging state, and
    also carries HTMX attributes for inline append."""
    a, item_ids = _seed_bookmarks_for_followee("a@e.com", "b@e.com", 30)
    r = a.get("/feed")
    assert r.status_code == 200
    # First page shows the newest 25 items, and NOT the tail.
    assert "Item 0" in r.text
    assert "Item 24" in r.text
    assert "Item 25" not in r.text
    # Load-more button present with both a JS-off href AND HTMX attrs
    # pointing at the partial endpoint.
    assert "Load more" in r.text
    assert "before=" in r.text
    assert "hx-get=" in r.text
    assert "partial=1" in r.text


def test_feed_second_page_shows_older_entries():
    a, item_ids = _seed_bookmarks_for_followee("a@e.com", "b@e.com", 30)
    r = a.get("/feed")
    # Pull the cursor out of the Load-more link (href, not hx-get).
    import re
    m = re.search(r'href="/feed\?before=([^"&]+)', r.text)
    assert m, "expected cursor link on first page"
    # Cursor is URL-encoded in the link (contains '+' from tz offset).
    # Pass it through verbatim — httpx will not re-encode.
    cursor_encoded = m.group(1)

    r2 = a.get(f"/feed?before={cursor_encoded}")
    assert r2.status_code == 200
    # Second page shows the tail we didn't see on page 1.
    assert "Item 25" in r2.text
    assert "Item 29" in r2.text
    # And no more Load-more button (5 items < page size).
    assert "Load more" not in r2.text


def test_feed_source_counts_reflect_real_totals_not_loaded_slice():
    """0.11.45 regression: when a viewer has more fediverse content
    than fits in the loaded window, the LOCAL chip badge used to under-
    count because it was derived from the merged top-N slice (fedi
    crowded local out on timestamps). Chip counts must reflect the real
    per-bucket totals over the 30-day window."""
    import app.db as db_mod
    # 40 local bookmarks from b (viewer a follows b).
    a, _ = _seed_bookmarks_for_followee("a@e.com", "b@e.com", 40)
    # 200 fediverse notes from a remote actor a follows, with very
    # recent timestamps so they'd otherwise dominate the merged slice.
    with Session(db_mod.engine) as s:
        u = s.exec(select(db_mod.User).where(db_mod.User.email == "a@e.com")).one()
        ra = db_mod.RemoteActor(
            actor_url="https://mastodon.example/users/loud",
            inbox_url="https://mastodon.example/users/loud/inbox",
            public_key_pem="-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----",
            preferred_username="loud",
        )
        s.add(ra)
        s.commit()
        s.refresh(ra)
        s.add(db_mod.RemoteFollowing(
            user_id=u.id, remote_actor_id=ra.id,
            follow_activity_id="https://local.test/a/1",
            accepted_at=datetime.now(UTC),
        ))
        s.commit()
        now = datetime.now(UTC)
        for i in range(200):
            s.add(db_mod.RemoteItem(
                remote_actor_id=ra.id,
                ap_note_id=f"https://mastodon.example/notes/{i}",
                content_html=f"<p>toot {i}</p>",
                note_url=f"https://mastodon.example/notes/{i}",
                published_at=now - timedelta(seconds=i),
            ))
        s.commit()

    r = a.get("/feed")
    assert r.status_code == 200
    body = r.text
    # Chip row present with distinct counts. The local badge must
    # reflect the FULL 40 local events (not the 0-ish that would land
    # on the first merged page against 200 louder fedi entries).
    assert 'class="feed-src-count">40<' in body
    assert 'class="feed-src-count">200<' in body
    # All = local + fedi.
    assert 'class="feed-src-count">240<' in body


def test_feed_partial_returns_just_the_fragment():
    """?partial=1 skips the base layout — used by HTMX to append inline
    when 'Load more' is clicked. Must contain the entries but NOT the
    surrounding page chrome (<html>, hero section, chip row)."""
    a, _ = _seed_bookmarks_for_followee("a@e.com", "b@e.com", 30)
    r = a.get("/feed?partial=1")
    assert r.status_code == 200
    assert "Item 0" in r.text
    # Fragment: no <html> shell, no hero heading.
    assert "<html" not in r.text
    assert "👥 Friends" not in r.text
    # Still carries a load-more button pointing at the next cursor.
    assert "Load more" in r.text


def test_feed_paginated_past_end_is_not_the_empty_onboarding():
    a, _ = _seed_bookmarks_for_followee("a@e.com", "b@e.com", 3)
    # Cursor set to way in the past → no matching entries.
    from urllib.parse import quote
    far_past = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    r = a.get(f"/feed?before={quote(far_past)}")
    assert r.status_code == 200
    assert "You've reached the end" in r.text
    # Should NOT show the first-run onboarding card in this state.
    assert "Your Friends page is quiet" not in r.text

