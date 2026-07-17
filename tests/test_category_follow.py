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


def test_follow_private_users_category_forbidden_without_access():
    """Category follow requires view access. Private users' categories
    are only reachable through an approved whole-user follow."""
    owner = _client()
    _login(owner, "farid@e.com")
    owner.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    cat_id, _, _ = _seed_category_with_feed("farid", "Secret", "https://a.example/rss")

    fan = _client()
    _login(fan, "bob@e.com")
    r = fan.post(f"/u/farid/c/{cat_id}/follow")
    assert r.status_code == 403


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
            s, farid_u, "favorited", "News-item",
            subject_url="https://x/news",
            ref_type="item", ref_id=news_item.id,
        )
        act_mod.record(
            s, farid_u, "favorited", "Blog-item",
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
