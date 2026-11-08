"""Public read API at /api/v1: visibility, field allowlist, schema snapshot."""

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

SNAPSHOT = Path(__file__).parent / "snapshots" / "api_openapi.json"


def _client():
    import app.main
    return TestClient(app.main.app)


def _seed(handle: str, visibility: str = "public"):
    from app.db import Category, Feed, Subscription, User, engine

    with Session(engine) as db:
        user = User(email=f"{handle}@example.com", handle=handle, visibility=visibility)
        db.add(user)
        db.commit()
        db.refresh(user)
        tech = Category(user_id=user.id, name="Tech", sort_order=0)
        news = Category(user_id=user.id, name="News", sort_order=1)
        db.add(tech)
        db.add(news)
        db.commit()
        for cat, url in ((tech, f"https://{handle}.example/tech.xml"),
                         (news, f"https://{handle}.example/news.xml")):
            feed = Feed(url=url, title=f"{cat.name} feed", site_url=f"https://{handle}.example")
            db.add(feed)
            db.commit()
            db.add(Subscription(user_id=user.id, feed_id=feed.id, category_id=cat.id,
                                note="secret owner note"))
        db.commit()
        db.refresh(user)
        return user


def test_instance():
    _seed("alice")
    r = _client().get("/api/v1/instance")
    assert r.status_code == 200
    body = r.json()
    assert body["users"] == 1
    assert body["feeds"] == 2
    assert body["api_version"] == "1"


def test_public_user_profile_categories_feeds():
    _seed("alice")
    c = _client()

    profile = c.get("/api/v1/users/alice").json()
    assert profile["handle"] == "alice"
    assert profile["url"] == "http://testserver/u/alice"
    assert profile["categories"] == 2
    assert profile["feeds"] == 2

    cats = c.get("/api/v1/users/alice/categories").json()
    assert cats == {"items": [{"name": "Tech", "feeds": 1}, {"name": "News", "feeds": 1}],
                    "next": None}

    feeds = c.get("/api/v1/users/alice/feeds").json()["items"]
    assert [f["category"] for f in feeds] == ["Tech", "News"]
    assert feeds[0]["url"] == "https://alice.example/tech.xml"
    assert feeds[0]["page_url"] == f"http://testserver/f/{feeds[0]['id']}"


def test_feeds_category_filter():
    _seed("alice")
    c = _client()
    feeds = c.get("/api/v1/users/alice/feeds", params={"category": "News"}).json()["items"]
    assert [f["title"] for f in feeds] == ["News feed"]
    assert c.get("/api/v1/users/alice/feeds", params={"category": "Nope"}).status_code == 404


def test_unlisted_user_fetchable_by_handle():
    _seed("ulla", visibility="unlisted")
    r = _client().get("/api/v1/users/ulla/feeds")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2


def test_private_user_is_404_like_unknown():
    _seed("pat", visibility="private")
    c = _client()
    for path in ("", "/categories", "/feeds"):
        private = c.get(f"/api/v1/users/pat{path}")
        unknown = c.get(f"/api/v1/users/nobody{path}")
        assert private.status_code == unknown.status_code == 404
        assert private.json() == unknown.json()


def test_private_user_hidden_even_with_owner_session():
    """The API ignores the session cookie, so a logged-in owner gets the
    same 404 as everyone else — nothing per-viewer can be cached."""
    from tests.test_public import _login

    c = _client()
    _login(c, "pat@example.com")
    c.post("/settings/visibility", data={"visibility": "private"}, follow_redirects=False)
    assert c.get("/u/pat").status_code == 200
    assert c.get("/api/v1/users/pat").status_code == 404


def test_responses_never_contain_secret_fields():
    user = _seed("alice")
    _seed_items()
    c = _client()
    bodies = [c.get(p).text for p in (
        "/api/v1/instance",
        "/api/v1/users/alice",
        "/api/v1/users/alice/categories",
        "/api/v1/users/alice/feeds",
        "/api/v1/users/alice/items",
        "/api/v1/users/alice/bookmarks",
        "/api/v1/users/alice/activity",
    )]
    for body in bodies:
        for needle in ("email", "activity_secret", "ap_private_key", "note",
                       "secret owner note", user.email, user.activity_secret):
            assert needle not in body, (needle, body)


def test_renamed_handle_redirects():
    from app.db import HandleAlias, engine

    user = _seed("alice")
    with Session(engine) as db:
        db.add(HandleAlias(old_handle="oldalice", user_id=user.id))
        db.commit()
    r = _client().get("/api/v1/users/oldalice/feeds?category=Tech", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/api/v1/users/alice/feeds?category=Tech"


def test_cors_allows_any_origin_without_credentials():
    _seed("alice")
    r = _client().get("/api/v1/users/alice", headers={"Origin": "https://elsewhere.example"})
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers


def test_api_docs_published_under_api_prefix():
    c = _client()
    assert c.get("/api/v1/docs").status_code == 200
    assert c.get("/api/v1/openapi.json").status_code == 200


def test_footer_links_to_api_docs():
    r = _client().get("/help")
    assert '<a href="/api/v1/docs">API</a>' in r.text


def test_help_documents_the_api():
    r = _client().get("/help")
    assert 'href="#api"' in r.text
    assert 'id="api"' in r.text


def test_openapi_schema_matches_snapshot():
    """Any change to the API's fields fails here until the snapshot is
    regenerated on purpose: RSSFRIENDS_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_api.py"""
    schema = _client().get("/api/v1/openapi.json").json()
    if os.getenv("RSSFRIENDS_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    assert schema == json.loads(SNAPSHOT.read_text())


def _seed_items(handle: str = "alice", per_feed: int = 3) -> None:
    """Items in both of the user's feeds. Two share a timestamp and one
    has no published_at, to exercise the cursor's tie-break and fallback."""
    from datetime import datetime, timedelta

    from app.db import Feed, FeedItem, engine

    base = datetime(2026, 1, 1, 12, 0)
    with Session(engine) as db:
        for feed in db.exec(select(Feed).where(Feed.url.startswith(f"https://{handle}."))).all():
            for i in range(per_feed):
                db.add(FeedItem(
                    feed_id=feed.id, guid=f"{feed.id}-{i}", title=f"{feed.title} #{i}",
                    link=f"https://{handle}.example/{feed.id}/{i}",
                    published_at=None if i == 0 else base + timedelta(hours=i),
                    fetched_at=base,
                ))
        db.commit()


def _all_pages(client, path: str, limit: int) -> list[dict]:
    items, cursor = [], None
    while True:
        params = {"limit": limit} | ({"cursor": cursor} if cursor else {})
        page = client.get(path, params=params).json()
        items += page["items"]
        cursor = page["next"]
        if cursor is None:
            return items


def test_items_newest_first_with_category():
    _seed("alice")
    _seed_items()
    items = _client().get("/api/v1/users/alice/items").json()["items"]
    assert len(items) == 6
    dates = [i["published_at"] for i in items]
    assert dates == sorted(dates, reverse=True)
    assert {i["category"] for i in items} == {"Tech", "News"}
    assert items[0]["feed_url"].endswith(".xml")

    news = _client().get("/api/v1/users/alice/items", params={"category": "News"}).json()
    assert {i["category"] for i in news["items"]} == {"News"}
    assert _client().get("/api/v1/users/alice/items?category=Nope").status_code == 404


def test_items_cursor_walks_every_item_once():
    _seed("alice")
    _seed_items(per_feed=5)
    c = _client()
    everything = c.get("/api/v1/users/alice/items", params={"limit": 100}).json()["items"]
    paged = _all_pages(c, "/api/v1/users/alice/items", limit=3)
    assert [i["id"] for i in paged] == [i["id"] for i in everything]
    assert len(paged) == 10


def test_items_cursor_stable_when_new_items_arrive():
    from datetime import datetime

    from app.db import Feed, FeedItem, engine

    _seed("alice")
    _seed_items()
    c = _client()
    first = c.get("/api/v1/users/alice/items", params={"limit": 2}).json()
    with Session(engine) as db:
        feed = db.exec(select(Feed)).first()
        db.add(FeedItem(feed_id=feed.id, guid="new", title="Brand new",
                        published_at=datetime(2030, 1, 1)))
        db.commit()
    second = c.get("/api/v1/users/alice/items",
                   params={"limit": 2, "cursor": first["next"]}).json()
    seen = {i["id"] for i in first["items"]}
    assert not seen & {i["id"] for i in second["items"]}
    assert "Brand new" not in [i["title"] for i in second["items"]]


def test_bad_cursor_and_limit_rejected():
    _seed("alice")
    c = _client()
    assert c.get("/api/v1/users/alice/items?cursor=not-a-cursor").status_code == 400
    assert c.get("/api/v1/users/alice/items?limit=101").status_code == 422
    assert c.get("/api/v1/users/alice/items?limit=0").status_code == 422


def test_bookmarks_paged():
    from datetime import datetime, timedelta

    from app.db import Bookmark, FeedItem, User, engine

    _seed("alice")
    _seed_items()
    with Session(engine) as db:
        alice = db.exec(select(User).where(User.handle == "alice")).one()
        for n, item in enumerate(db.exec(select(FeedItem)).all()):
            db.add(Bookmark(user_id=alice.id, feed_item_id=item.id,
                            created_at=datetime(2026, 2, 1) + timedelta(minutes=n)))
        db.commit()
    c = _client()
    paged = _all_pages(c, "/api/v1/users/alice/bookmarks", limit=4)
    assert len(paged) == 6
    marks = [b["bookmarked_at"] for b in paged]
    assert marks == sorted(marks, reverse=True)


def test_activity_hides_follows_of_private_users():
    from app.db import Activity, User, engine

    _seed("alice")
    _seed("pat", visibility="private")
    _seed("bob")
    with Session(engine) as db:
        users = {u.handle: u for u in db.exec(select(User)).all()}
        for target in ("pat", "bob"):
            db.add(Activity(user_id=users["alice"].id, kind="followed", subject=f"@{target}",
                            subject_url=f"/u/{target}", ref_type="user",
                            ref_id=users[target].id))
        db.add(Activity(user_id=users["alice"].id, kind="created_category", subject="Tech",
                        ref_type="category", ref_id=1))
        db.commit()
    body = _client().get("/api/v1/users/alice/activity").json()
    assert sorted(a["subject"] for a in body["items"]) == ["@bob", "Tech"]
    assert "pat" not in json.dumps(body)


def test_private_user_paged_endpoints_404():
    _seed("pat", visibility="private")
    c = _client()
    for path in ("/items", "/bookmarks", "/activity"):
        assert c.get(f"/api/v1/users/pat{path}").status_code == 404


def test_collections_public_only():
    from app.db import Collection, CollectionFeed, Feed, engine

    with Session(engine) as db:
        feed = Feed(url="https://berlin.example/feed.xml", title="Berlin")
        shown = Collection(slug="berlin", name="Berlin", is_official=True)
        hidden = Collection(slug="draft", name="Draft", is_public=False)
        db.add_all([feed, shown, hidden])
        db.commit()
        db.add(CollectionFeed(collection_id=shown.id, feed_id=feed.id, note="city news"))
        db.commit()
    c = _client()
    listing = c.get("/api/v1/collections").json()["items"]
    assert [col["slug"] for col in listing] == ["berlin"]
    assert listing[0]["feed_count"] == 1

    detail = c.get("/api/v1/collections/berlin").json()
    assert detail["feeds"][0]["url"] == "https://berlin.example/feed.xml"
    assert detail["feeds"][0]["note"] == "city news"
    assert c.get("/api/v1/collections/draft").status_code == 404
    assert c.get("/api/v1/collections/nope").status_code == 404
