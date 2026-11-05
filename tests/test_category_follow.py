"""Follow a single category on someone else's radar. Category follows
are a finer-grained alternative to whole-user Follow; only item-scoped
activity flows through them."""

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


def _uid(handle: str) -> int:
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        return s.exec(select(db_mod.User).where(db_mod.User.handle == handle)).one().id


def _seed_category_with_feed(handle: str, cat_name: str, feed_url: str) -> tuple[int, int, int]:
    """Create a category on user's radar with one subscribed feed.
    Returns (cat_id, sub_id, feed_id)."""
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        uid = s.exec(select(db_mod.User).where(db_mod.User.handle == handle)).one().id
        max_order = s.exec(
            select(db_mod.Category.sort_order)
            .where(db_mod.Category.user_id == uid)
            .order_by(db_mod.Category.sort_order.desc())
        ).first() or 0
        cat = db_mod.Category(user_id=uid, name=cat_name, sort_order=max_order + 1)
        s.add(cat)
        s.commit()
        s.refresh(cat)
        feed = db_mod.Feed(url=feed_url, title=feed_url)
        s.add(feed)
        s.commit()
        s.refresh(feed)
        sub = db_mod.Subscription(
            user_id=uid, feed_id=feed.id, category_id=cat.id, sort_order=1
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return cat.id, sub.id, feed.id


# ------------- CRUD -------------

def test_follow_category_requires_login():
    r = _client().post("/u/anyone/c/1/follow")
    assert r.status_code == 401


def test_follow_own_category_rejected():
    c = _client()
    _login(c, "farid@e.com")
    cat_id, _, _ = _seed_category_with_feed("farid", "News", "https://a.example/rss")
    r = c.post(f"/u/farid/c/{cat_id}/follow")
    assert r.status_code == 400


def test_follow_public_users_category():
    owner = _client()
    _login(owner, "farid@e.com")
    cat_id, _, _ = _seed_category_with_feed("farid", "News", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    r = fan.post(f"/u/farid/c/{cat_id}/follow")
    assert r.status_code == 200
    assert "✓ Following category" in r.text

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        rows = s.exec(select(db_mod.CategoryFollow).where(
            db_mod.CategoryFollow.follower_id == _uid("bob")
        )).all()
    assert len(rows) == 1
    assert rows[0].category_id == cat_id


def test_follow_category_also_adds_readable_subscription():
    """0.2.43: following a category also subscribes the follower to
    the category's RSS URL, in a 'Following' category on their radar.
    The user can then Move / Edit URL / Remove that row like any
    other feed."""
    owner = _client()
    _login(owner, "farid@e.com")
    cat_id, _, _ = _seed_category_with_feed("farid", "Generic", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    fan.post(f"/u/farid/c/{cat_id}/follow")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        bob_id = _uid("bob")
        # A 'Following' category exists on bob's radar.
        following = s.exec(
            select(db_mod.Category)
            .where(db_mod.Category.user_id == bob_id)
            .where(db_mod.Category.name == "Following")
        ).first()
        assert following is not None
        # A Subscription points at farid's Generic RSS URL.
        rss_url_frag = "/u/farid/c/Generic.rss"
        subs = s.exec(
            select(db_mod.Subscription, db_mod.Feed)
            .join(db_mod.Feed, db_mod.Feed.id == db_mod.Subscription.feed_id)
            .where(db_mod.Subscription.user_id == bob_id)
        ).all()
        matching = [f.url for _sub, f in subs if rss_url_frag in f.url]
        assert len(matching) == 1


def test_unfollow_category_removes_readable_subscription():
    owner = _client()
    _login(owner, "farid@e.com")
    cat_id, _, _ = _seed_category_with_feed("farid", "News", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    fan.post(f"/u/farid/c/{cat_id}/follow")
    fan.delete(f"/u/farid/c/{cat_id}/follow")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        bob_id = _uid("bob")
        subs = s.exec(
            select(db_mod.Subscription, db_mod.Feed)
            .join(db_mod.Feed, db_mod.Feed.id == db_mod.Subscription.feed_id)
            .where(db_mod.Subscription.user_id == bob_id)
        ).all()
        assert not [f.url for _sub, f in subs if "/u/farid/c/News.rss" in f.url]


def test_unfollow_category():
    owner = _client()
    _login(owner, "farid@e.com")
    cat_id, _, _ = _seed_category_with_feed("farid", "News", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    fan.post(f"/u/farid/c/{cat_id}/follow")
    r = fan.delete(f"/u/farid/c/{cat_id}/follow")
    assert r.status_code == 200
    assert "🔔 Follow category" in r.text

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        rows = s.exec(select(db_mod.CategoryFollow)).all()
    assert rows == []


def test_follow_category_idempotent():
    owner = _client()
    _login(owner, "farid@e.com")
    cat_id, _, _ = _seed_category_with_feed("farid", "News", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    fan.post(f"/u/farid/c/{cat_id}/follow")
    fan.post(f"/u/farid/c/{cat_id}/follow")  # again

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        rows = s.exec(select(db_mod.CategoryFollow)).all()
    assert len(rows) == 1


def _approve(follower: str, owner: str) -> None:
    from datetime import UTC, datetime

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        follow = s.get(db_mod.Follow, (_uid(follower), _uid(owner)))
        follow.approved_at = datetime.now(UTC)
        s.add(follow)
        s.commit()


def _private_owner_with_linked_fan():
    """pesarkhobeee (private) has a 'Twitter' category; farid is an
    approved follower who linked it. Returns (owner, fan, cat_id)."""
    owner = _client()
    _login(owner, "pesarkhobeee@e.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    cat_id, _, _ = _seed_category_with_feed(
        "pesarkhobeee", "Twitter", "https://secret.example/rss"
    )
    fan = _client()
    _login(fan, "farid@e.com")
    fan.post("/u/pesarkhobeee/follow")
    _approve("farid", "pesarkhobeee")
    r = fan.post(f"/u/pesarkhobeee/c/{cat_id}/follow")
    assert r.status_code == 200
    return owner, fan, cat_id


def test_follow_private_category_requires_approved_follow():
    owner = _client()
    _login(owner, "farid@e.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    cat_id, _, _ = _seed_category_with_feed("farid", "Secret", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    assert fan.post(f"/u/farid/c/{cat_id}/follow").status_code == 403

    fan.post("/u/farid/follow")  # pending
    assert fan.post(f"/u/farid/c/{cat_id}/follow").status_code == 403


def test_follow_private_category_creates_no_subscription():
    _owner, _fan, _cat_id = _private_owner_with_linked_fan()
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        assert len(s.exec(select(db_mod.CategoryFollow)).all()) == 1
        farid_subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == _uid("farid"))
        ).all()
    assert farid_subs == []


def test_follow_category_button_shows_for_approved_follower_on_private_radar():
    owner = _client()
    _login(owner, "farid@e.com")
    _seed_category_with_feed("farid", "News", "https://a.example/rss")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    fan = _client()
    _login(fan, "bob@e.com")
    fan.post("/u/farid/follow")
    _approve("bob", "farid")

    r = fan.get("/u/farid?cat=News")
    assert r.status_code == 200
    assert "🔔 Follow category" in r.text
    assert "/u/farid/c/News.rss" not in r.text


def test_linked_category_visible_only_to_follower_on_own_radar():
    _owner, fan, cat_id = _private_owner_with_linked_fan()

    r = fan.get("/u/farid")
    assert "🔒 @pesarkhobeee · Twitter" in r.text
    assert r.headers["cache-control"] == "private, no-store"

    r = fan.get(f"/u/farid?linked={cat_id}")
    assert r.status_code == 200
    assert "https://secret.example/rss" in r.text

    anon = _client().get("/u/farid")
    assert "@pesarkhobeee" not in anon.text
    assert _client().get(f"/u/farid?linked={cat_id}").status_code == 404

    other = _client()
    _login(other, "alice@e.com")
    assert "@pesarkhobeee" not in other.get("/u/farid").text
    assert other.get(f"/u/farid?linked={cat_id}").status_code == 404


def test_linked_category_absent_from_follower_exports():
    _owner, fan, _cat_id = _private_owner_with_linked_fan()
    for path in ("/u/farid.opml", "/u/farid.rss", "/u/farid/activity"):
        r = fan.get(path)
        assert "secret.example" not in r.text, path
        assert "Twitter" not in r.text, path


def test_linked_category_card_has_no_copy_or_bookmark_actions():
    _owner, fan, cat_id = _private_owner_with_linked_fan()
    r = fan.get(f"/u/farid?linked={cat_id}")
    assert "/items/" not in r.text
    assert "/f/" not in r.text
    assert "/subscriptions/" not in r.text


def test_linked_category_id_not_followed_is_404():
    owner, fan, _cat_id = _private_owner_with_linked_fan()
    other_cat, _, _ = _seed_category_with_feed(
        "pesarkhobeee", "Hidden", "https://hidden.example/rss"
    )
    assert fan.get(f"/u/farid?linked={other_cat}").status_code == 404


def test_revoke_hides_linked_category_and_drops_row():
    owner, fan, cat_id = _private_owner_with_linked_fan()
    r = owner.post(f"/followers/{_uid('farid')}/revoke")
    assert r.status_code == 200

    assert "@pesarkhobeee" not in fan.get("/u/farid").text
    assert fan.get(f"/u/farid?linked={cat_id}").status_code == 404
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        assert s.exec(select(db_mod.CategoryFollow)).all() == []


def test_unfollow_private_owner_drops_linked_category():
    _owner, fan, _cat_id = _private_owner_with_linked_fan()
    fan.delete("/u/pesarkhobeee/follow")
    assert "@pesarkhobeee" not in fan.get("/u/farid").text
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        assert s.exec(select(db_mod.CategoryFollow)).all() == []


def test_timeline_ignores_category_follow_after_access_lost():
    """A leftover CategoryFollow row must not leak a private owner's
    activity once the Follow is gone."""
    _owner, fan, _cat_id = _private_owner_with_linked_fan()
    import app.db as db_mod
    from app import activity as activity_mod
    with Session(db_mod.engine) as s:
        owner_user = s.get(db_mod.User, _uid("pesarkhobeee"))
        feed = s.exec(
            select(db_mod.Feed).where(db_mod.Feed.url == "https://secret.example/rss")
        ).one()
        activity_mod.record(
            s, owner_user, "subscribed", "SecretFeedTitle",
            subject_url=feed.url, ref_type="feed", ref_id=feed.id,
        )
        s.delete(s.get(db_mod.Follow, (_uid("farid"), _uid("pesarkhobeee"))))
        s.commit()

    r = fan.get("/feed")
    assert r.status_code == 200
    assert "SecretFeedTitle" not in r.text


def test_follow_unknown_category_404s():
    c = _client()
    _login(c, "farid@e.com")
    _login(_client(), "bob@e.com")  # owner
    fan = _client()
    _login(fan, "alice@e.com")
    r = fan.post("/u/bob/c/999999/follow")
    assert r.status_code == 404


# ------------- Timeline aggregation -------------

def test_category_follow_shows_subscribes_from_that_category_only():
    """When @bob follows @farid's News category, and farid subscribes
    to a feed inside News, bob's /feed sees it. But if farid subscribes
    to something inside a DIFFERENT category, bob does NOT see it via
    the category follow."""
    import app.db as db_mod
    from app import activity as act_mod

    farid = _client()
    _login(farid, "farid@e.com")
    news_cat, _, news_feed = _seed_category_with_feed("farid", "News", "https://news.example/rss")
    weblog_cat, _, weblog_feed = _seed_category_with_feed("farid", "Weblogs", "https://blog.example/rss")

    # Farid's activity events reference feeds by ref_type='feed', ref_id=feed_id.
    with Session(db_mod.engine) as s:
        farid_u = s.exec(select(db_mod.User).where(db_mod.User.handle == "farid")).one()
        act_mod.record(
            s, farid_u, "subscribed", "News Feed",
            subject_url="https://news.example",
            ref_type="feed", ref_id=news_feed,
        )
        act_mod.record(
            s, farid_u, "subscribed", "Weblog Feed",
            subject_url="https://blog.example",
            ref_type="feed", ref_id=weblog_feed,
        )
        s.commit()

    bob = _client()
    _login(bob, "bob@e.com")
    bob.post(f"/u/farid/c/{news_cat}/follow")

    r = bob.get("/feed")
    assert r.status_code == 200
    # News subscribe reaches bob via the category follow…
    assert "News Feed" in r.text
    # …but Weblogs subscribe does NOT (bob doesn't follow that category
    # and doesn't whole-user-follow farid).
    assert "Weblog Feed" not in r.text


def test_category_follow_shows_favorites_from_that_category():
    """Farid stars an item on a News feed → bob (who follows News) sees
    it. Farid stars an item on a Weblog feed → bob doesn't."""
    import app.db as db_mod
    from app import activity as act_mod

    farid = _client()
    _login(farid, "farid@e.com")
    news_cat, _, news_feed = _seed_category_with_feed("farid", "News", "https://news.example/rss")
    weblog_cat, _, weblog_feed = _seed_category_with_feed("farid", "Weblogs", "https://blog.example/rss")

    with Session(db_mod.engine) as s:
        news_item = db_mod.FeedItem(feed_id=news_feed, guid="n1", title="News-item")
        blog_item = db_mod.FeedItem(feed_id=weblog_feed, guid="b1", title="Blog-item")
        s.add(news_item)
        s.add(blog_item)
        s.commit()
        s.refresh(news_item)
        s.refresh(blog_item)
        farid_u = s.exec(select(db_mod.User).where(db_mod.User.handle == "farid")).one()
        act_mod.record(
            s, farid_u, "bookmarked", "News-item",
            subject_url="https://x/news",
            ref_type="item", ref_id=news_item.id,
        )
        act_mod.record(
            s, farid_u, "bookmarked", "Blog-item",
            subject_url="https://x/blog",
            ref_type="item", ref_id=blog_item.id,
        )
        s.commit()

    bob = _client()
    _login(bob, "bob@e.com")
    bob.post(f"/u/farid/c/{news_cat}/follow")

    r = bob.get("/feed")
    assert "News-item" in r.text
    assert "Blog-item" not in r.text


def test_category_follow_ignores_person_scoped_activity():
    """Follows / forks / created_category / posted are person-scoped —
    they reach followers ONLY via whole-user follow, not via category
    follow (there's no category to scope them to)."""
    import app.db as db_mod
    from app import activity as act_mod

    farid = _client()
    _login(farid, "farid@e.com")
    news_cat, _, _ = _seed_category_with_feed("farid", "News", "https://a.example/rss")

    with Session(db_mod.engine) as s:
        farid_u = s.exec(select(db_mod.User).where(db_mod.User.handle == "farid")).one()
        act_mod.record(
            s, farid_u, "forked", "alice",
            subject_url="/u/alice", ref_type="user", ref_id=999,
        )
        s.commit()

    bob = _client()
    _login(bob, "bob@e.com")
    bob.post(f"/u/farid/c/{news_cat}/follow")

    r = bob.get("/feed")
    # No forked-activity leakage through category follow.
    assert "forked" not in r.text
    assert "alice" not in r.text


def test_whole_user_and_category_follow_dont_duplicate():
    """Whole-user Follow and CategoryFollow overlap on item-scoped
    activity. The timeline must not show the same event twice."""
    import app.db as db_mod
    from app import activity as act_mod

    farid = _client()
    _login(farid, "farid@e.com")
    news_cat, _, news_feed = _seed_category_with_feed("farid", "News", "https://news.example/rss")

    with Session(db_mod.engine) as s:
        farid_u = s.exec(select(db_mod.User).where(db_mod.User.handle == "farid")).one()
        act_mod.record(
            s, farid_u, "subscribed", "News Feed",
            subject_url="https://news.example",
            ref_type="feed", ref_id=news_feed,
        )
        s.commit()

    bob = _client()
    _login(bob, "bob@e.com")
    bob.post("/u/farid/follow")
    bob.post(f"/u/farid/c/{news_cat}/follow")

    r = bob.get("/feed")
    assert r.text.count("News Feed") == 1


# ------------- Per-category RSS -------------

def test_per_category_rss_returns_only_that_categorys_items():
    owner = _client()
    _login(owner, "farid@e.com")
    news_cat, _, news_feed = _seed_category_with_feed("farid", "News", "https://news.example/rss")
    _, _, blog_feed = _seed_category_with_feed("farid", "Weblogs", "https://blog.example/rss")

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        s.add(db_mod.FeedItem(feed_id=news_feed, guid="n1", title="Big-Newsy-Thing",
                              link="https://news.example/1"))
        s.add(db_mod.FeedItem(feed_id=blog_feed, guid="b1", title="Blogpost-Item",
                              link="https://blog.example/1"))
        s.commit()

    r = _client().get("/u/farid/c/News.rss")
    assert r.status_code == 200
    assert "Big-Newsy-Thing" in r.text
    assert "Blogpost-Item" not in r.text
    assert "application/rss" in r.headers.get("content-type", "")


def test_per_category_rss_404_for_unknown_category():
    owner = _client()
    _login(owner, "farid@e.com")
    r = _client().get("/u/farid/c/DoesNotExist.rss")
    assert r.status_code == 404


def test_per_category_rss_gated_by_privacy():
    owner = _client()
    _login(owner, "farid@e.com")
    _seed_category_with_feed("farid", "Secret", "https://a.example/rss")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)

    r = _client().get("/u/farid/c/Secret.rss")
    assert r.status_code == 403


# ------------- UI: Follow-category button on radar -------------

def test_follow_category_button_shows_for_signed_in_visitor():
    owner = _client()
    _login(owner, "farid@e.com")
    _seed_category_with_feed("farid", "News", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    r = fan.get("/u/farid?cat=News")
    assert r.status_code == 200
    assert "🔔 Follow category" in r.text
    assert "/u/farid/c/News.rss" in r.text  # per-category RSS link too


def test_follow_category_button_hidden_from_owner():
    owner = _client()
    _login(owner, "farid@e.com")
    _seed_category_with_feed("farid", "News", "https://a.example/rss")
    r = owner.get("/u/farid?cat=News")
    assert "🔔 Follow category" not in r.text


def test_removing_category_follow_subscription_from_dashboard_drops_category_follow():
    """@julian-lamas bug 2026-08-02: after following farid's 'Github feeds'
    category, then removing the auto-added subscription from /dashboard,
    the CategoryFollow row must also be dropped — otherwise every deploy's
    startup re-scan (before this fix) or any future re-follow would
    silently re-add the subscription."""
    from sqlmodel import Session, select

    import app.db as db_mod

    # Setup: farid publishes a Github-feeds category with a feed.
    farid = _client()
    _login(farid, "farid@e.com")
    _seed_category_with_feed("farid", "Github feeds", "https://gh.example/rss")

    # bob follows it → CategoryFollow row + auto-added Subscription created.
    bob = _client()
    _login(bob, "bob@e.com")
    with Session(db_mod.engine) as s:
        cat = s.exec(select(db_mod.Category)
                     .where(db_mod.Category.name == "Github feeds")).one()
    r = bob.post(f"/u/farid/c/{cat.id}/follow")
    assert r.status_code == 200

    # Find the Subscription for the category-follow feed on bob's radar.
    with Session(db_mod.engine) as s:
        bob_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "bob")).one()
        sub = s.exec(
            select(db_mod.Subscription)
            .where(db_mod.Subscription.user_id == bob_row.id)
        ).one()
        sub_id = sub.id

    # bob deletes the subscription via the dashboard.
    r = bob.delete(f"/subscriptions/{sub_id}")
    assert r.status_code == 200

    # BOTH rows should be gone (bob's — farid's own sub is untouched).
    with Session(db_mod.engine) as s:
        bob_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "bob")).one()
        bob_subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == bob_row.id)
        ).all()
        cfs = s.exec(
            select(db_mod.CategoryFollow).where(db_mod.CategoryFollow.follower_id == bob_row.id)
        ).all()
        assert len(bob_subs) == 0, "bob's Subscription should be deleted"
        assert len(cfs) == 0, "bob's CategoryFollow should be dropped too"


def test_removing_non_category_follow_sub_leaves_category_follow_alone():
    """Regression guard: unsubscribing from a normal (non-category-follow)
    feed doesn't touch any CategoryFollow rows. The URL-pattern gate is
    tight enough to only match /u/{h}/c/{cat}.rss."""
    from sqlmodel import Session, select

    import app.db as db_mod

    # bob has a normal subscription to a normal feed AND follows farid's
    # category (which creates a second sub + a CategoryFollow row).
    farid = _client()
    _login(farid, "farid@e.com")
    _seed_category_with_feed("farid", "Tech", "https://tech.example/rss")

    bob = _client()
    _login(bob, "bob@e.com")

    with Session(db_mod.engine) as s:
        cat = s.exec(select(db_mod.Category).where(db_mod.Category.name == "Tech")).one()
    bob.post(f"/u/farid/c/{cat.id}/follow")

    # bob adds a normal subscription in some category on his own radar.
    r = bob.post("/categories", data={"name": "Personal"})
    idx = r.text.find('id="category-') + len('id="category-')
    end = r.text.find('"', idx)
    personal_cat_id = int(r.text[idx:end])
    with Session(db_mod.engine) as s:
        bob_row = s.exec(select(db_mod.User).where(db_mod.User.handle == "bob")).one()
        feed = db_mod.Feed(url="https://normal.example/rss", title="Normal")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        normal_sub = db_mod.Subscription(
            user_id=bob_row.id, feed_id=feed.id,
            category_id=personal_cat_id, sort_order=1,
        )
        s.add(normal_sub)
        s.commit()
        s.refresh(normal_sub)
        normal_sub_id = normal_sub.id

    # Remove the NORMAL sub → CategoryFollow untouched.
    bob.delete(f"/subscriptions/{normal_sub_id}")
    with Session(db_mod.engine) as s:
        assert len(s.exec(select(db_mod.CategoryFollow)).all()) == 1
