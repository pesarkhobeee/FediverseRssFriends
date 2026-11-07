"""Public read API at /api/v1: visibility, field allowlist, schema snapshot."""

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

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
    c = _client()
    bodies = [c.get(p).text for p in (
        "/api/v1/instance",
        "/api/v1/users/alice",
        "/api/v1/users/alice/categories",
        "/api/v1/users/alice/feeds",
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


def test_openapi_schema_matches_snapshot():
    """Any change to the API's fields fails here until the snapshot is
    regenerated on purpose: RSSFRIENDS_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_api.py"""
    schema = _client().get("/api/v1/openapi.json").json()
    if os.getenv("RSSFRIENDS_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    assert schema == json.loads(SNAPSHOT.read_text())
