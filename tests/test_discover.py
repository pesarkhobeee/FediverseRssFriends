"""Discover page: sections + public-only filtering + rankings."""

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


def test_discover_open_to_anonymous():
    r = _client().get("/discover")
    assert r.status_code == 200
    assert "Recent radars" in r.text


def test_discover_empty_state():
    r = _client().get("/discover")
    assert r.status_code == 200
    assert "No public radars yet" in r.text
    assert "No feeds have been subscribed" in r.text
    assert "Nobody has been forked yet" in r.text


def test_recent_users_shows_only_public():
    a = _client()
    _login(a, "public@e.com")

    b = _client()
    _login(b, "unlisted@e.com")
    b.post("/settings/visibility", data={"visibility": "unlisted"}, follow_redirects=False)

    c = _client()
    _login(c, "private@e.com")
    c.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    r = _client().get("/discover")
    assert "@public" in r.text
    assert "@unlisted" not in r.text
    assert "@private" not in r.text


def test_popular_feeds_by_subscriber_count():
    """Two feeds; one has more subscribers → appears first."""
    a = _client()
    _login(a, "a@e.com")
    b = _client()
    _login(b, "b@e.com")

    # Seed two feeds and subscriptions directly.
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        popular = db_mod.Feed(url="https://pop.example/feed", title="Popular Feed")
        s.add(popular)
        niche = db_mod.Feed(url="https://niche.example/feed", title="Niche Feed")
        s.add(niche)
        s.commit()
        s.refresh(popular)
        s.refresh(niche)

        a_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "a")).one()
        b_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "b")).one()

        cat_a = db_mod.Category(user_id=a_row.id, name="c", sort_order=1)
        cat_b = db_mod.Category(user_id=b_row.id, name="c", sort_order=1)
        s.add(cat_a)
        s.add(cat_b)
        s.commit()
        s.refresh(cat_a)
        s.refresh(cat_b)

        # Both users subscribe to Popular Feed.
        s.add(db_mod.Subscription(
            user_id=a_row.id, feed_id=popular.id, category_id=cat_a.id, sort_order=1
        ))
        s.add(db_mod.Subscription(
            user_id=b_row.id, feed_id=popular.id, category_id=cat_b.id, sort_order=1
        ))
        # Only A subscribes to Niche Feed.
        s.add(db_mod.Subscription(
            user_id=a_row.id, feed_id=niche.id, category_id=cat_a.id, sort_order=2
        ))
        s.commit()

    r = _client().get("/discover")
    idx_pop = r.text.find("Popular Feed")
    idx_niche = r.text.find("Niche Feed")
    assert idx_pop > 0 and idx_niche > 0
    assert idx_pop < idx_niche, "popular feed should be listed first"
    assert "2 subscribers" in r.text
    assert "1 subscriber" in r.text


def test_popular_feeds_count_includes_private_user_subs():
    """The aggregate popularity count includes every subscriber regardless
    of visibility — a total N doesn't identify anyone."""
    a = _client()
    _login(a, "public@e.com")
    p = _client()
    _login(p, "private@e.com")
    p.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="A Feed")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        pu = s.exec(select(db_mod.User).where(db_mod.User.handle == "public")).one()
        pr = s.exec(select(db_mod.User).where(db_mod.User.handle == "private")).one()
        cat_pu = db_mod.Category(user_id=pu.id, name="c", sort_order=1)
        cat_pr = db_mod.Category(user_id=pr.id, name="c", sort_order=1)
        s.add(cat_pu)
        s.add(cat_pr)
        s.commit()
        s.refresh(cat_pu)
        s.refresh(cat_pr)
        s.add(db_mod.Subscription(
            user_id=pu.id, feed_id=feed.id, category_id=cat_pu.id, sort_order=1
        ))
        s.add(db_mod.Subscription(
            user_id=pr.id, feed_id=feed.id, category_id=cat_pr.id, sort_order=1
        ))
        s.commit()

    r = _client().get("/discover")
    assert "2 subscribers" in r.text


def test_popular_feeds_hides_feeds_only_subscribed_by_private_users():
    """A feed followed exclusively by private users should NOT surface
    on Discover — otherwise anyone can infer that at least one private
    user follows it. Once any non-private user subscribes, it can
    appear (and the count includes the private users too)."""
    p = _client()
    _login(p, "private@e.com")
    p.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    import app.db as db_mod

    with Session(db_mod.engine) as s:
        hidden = db_mod.Feed(url="https://hidden.example/feed", title="Private-Only Feed")
        s.add(hidden)
        s.commit()
        s.refresh(hidden)
        pr = s.exec(select(db_mod.User).where(db_mod.User.handle == "private")).one()
        cat = db_mod.Category(user_id=pr.id, name="c", sort_order=1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        s.add(db_mod.Subscription(
            user_id=pr.id, feed_id=hidden.id, category_id=cat.id, sort_order=1
        ))
        s.commit()

    r = _client().get("/discover")
    assert "Private-Only Feed" not in r.text

    # Now a public user subscribes too — the feed becomes listable.
    a = _client()
    _login(a, "public@e.com")
    with Session(db_mod.engine) as s:
        pu = s.exec(select(db_mod.User).where(db_mod.User.handle == "public")).one()
        hidden = s.exec(
            select(db_mod.Feed).where(db_mod.Feed.title == "Private-Only Feed")
        ).one()
        cat_pu = db_mod.Category(user_id=pu.id, name="c", sort_order=1)
        s.add(cat_pu)
        s.commit()
        s.refresh(cat_pu)
        s.add(db_mod.Subscription(
            user_id=pu.id, feed_id=hidden.id, category_id=cat_pu.id, sort_order=1
        ))
        s.commit()

    r = _client().get("/discover")
    assert "Private-Only Feed" in r.text
    # Count reflects BOTH subscribers (private + public).
    assert "2 subscribers" in r.text


def test_search_finds_exact_handle():
    a = _client()
    _login(a, "alice@e.com")

    r = _client().get("/discover/search?q=alice")
    assert r.status_code == 200
    assert "@alice" in r.text


def test_search_is_fuzzy_and_handles_typos():
    """Typo `aliec` for `alice` should still find her."""
    a = _client()
    _login(a, "alice@e.com")
    b = _client()
    _login(b, "bob@e.com")

    r = _client().get("/discover/search?q=aliec")  # typo
    assert r.status_code == 200
    assert "@alice" in r.text


def test_search_substring_match():
    a = _client()
    _login(a, "farid-ahmadian@e.com")

    r = _client().get("/discover/search?q=ahmad")
    assert r.status_code == 200
    assert "@farid-ahmadian" in r.text


def test_search_excludes_private_and_unlisted():
    a = _client()
    _login(a, "alice-public@e.com")
    b = _client()
    _login(b, "alice-private@e.com")
    b.post(
        "/settings/visibility", data={"visibility": "private"}, follow_redirects=False
    )
    c = _client()
    _login(c, "alice-unlisted@e.com")
    c.post(
        "/settings/visibility", data={"visibility": "unlisted"}, follow_redirects=False
    )

    r = _client().get("/discover/search?q=alice")
    assert "@alice-public" in r.text
    assert "@alice-private" not in r.text
    assert "@alice-unlisted" not in r.text


def test_search_no_match_shows_hint():
    r = _client().get("/discover/search?q=zzzzzzz-nobody")
    assert r.status_code == 200
    assert "No public radars match" in r.text


def test_search_empty_query_returns_nothing():
    r = _client().get("/discover/search?q=")
    assert r.status_code == 200
    assert "No public radars match" not in r.text


def test_search_ignores_leading_at():
    a = _client()
    _login(a, "alice@e.com")
    r = _client().get("/discover/search?q=@alice")
    assert "@alice" in r.text


def test_discover_page_has_search_input():
    r = _client().get("/discover")
    assert 'hx-get="/discover/search"' in r.text
    assert "Find a friend" in r.text


def test_discover_layout_has_new_structure():
    """Sanity check that the 0.7.1 redesign markup renders — two-column
    grid, hero, and avatars on radar rows. If any of these grep-strings
    disappear, the layout has regressed."""
    a = _client()
    _login(a, "a@e.com")

    r = _client().get("/discover")
    assert r.status_code == 200
    body = r.text
    assert 'class="discover-grid"' in body        # two-column layout
    assert 'class="discover-hero"' in body        # editorial hero
    assert "discover-radar-list" in body          # radar rows container
    assert 'class="user-avatar"' in body          # colored initials avatar
    assert ">A<" in body                          # @a's uppercase initial rendered


def test_most_forked_curators_ranking():
    """User with more fork activity events ranks first."""
    a = _client()
    _login(a, "a@e.com")
    # a has some feeds so fork actually copies things.
    r = a.post("/categories", data={"name": "News"})
    idx = r.text.find('id="category-') + len('id="category-')
    end = r.text.find('"', idx)
    cat_id = int(r.text[idx:end])

    # Give a a feed by inserting directly.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://a.example/feed", title="From A")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        a_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "a")).one()
        s.add(db_mod.Subscription(
            user_id=a_row.id, feed_id=feed.id, category_id=cat_id, sort_order=1
        ))
        s.commit()

    b = _client()
    _login(b, "b@e.com")
    b.post("/u/a/fork")

    c = _client()
    _login(c, "c@e.com")
    c.post("/u/a/fork")

    d = _client()
    _login(d, "d@e.com")
    # d also gets forked once by b:
    d.post("/categories", data={"name": "News"})
    with Session(db_mod.engine) as s:
        d_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "d")).one()
        feed2 = db_mod.Feed(url="https://d.example/feed", title="From D")
        s.add(feed2)
        s.commit()
        s.refresh(feed2)
        cat = s.exec(select(db_mod.Category).where(db_mod.Category.user_id == d_row.id)).first()
        s.add(db_mod.Subscription(
            user_id=d_row.id, feed_id=feed2.id, category_id=cat.id, sort_order=1
        ))
        s.commit()
    b.post("/u/d/fork")

    r = _client().get("/discover")
    # Both appear at least once (recent + forks lists). Count is rendered
    # inline in the pill badge as "forked ×N" (compact form since 0.7.1).
    assert "forked ×2" in r.text  # @a was forked twice
    assert "forked ×1" in r.text  # @d was forked once
    # And @a's fork line appears before @d's in the "Most-forked" section.
    section = r.text[r.text.find("Most-forked"):]
    assert section.find("@a") < section.find("@d")
