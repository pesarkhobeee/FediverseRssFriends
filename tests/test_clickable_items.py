"""Whole-item tap targets on /bookmarks, /activity, and /feed.

The `.item-clickable` class opts a row into the stretched-link pattern
so mobile users can tap anywhere on the row to open the referenced
link. Guarded by URL presence — rows with no `subject_url` (e.g.
`created_category` activity) stay non-tappable so we don't fake a
dead click target."""

from fastapi.testclient import TestClient
from sqlmodel import Session


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


def _seed_feed_and_item():
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(url="https://x.example/feed", title="Example")
        s.add(feed)
        s.commit()
        s.refresh(feed)
        item = db_mod.FeedItem(
            feed_id=feed.id, guid="urn:1",
            title="First post", link="https://x.example/1",
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        return feed.id, item.id


def test_bookmarks_page_marks_rows_clickable():
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")
    client.post(f"/items/{item_id}/bookmark")

    r = client.get("/u/alice/bookmarks")
    assert r.status_code == 200
    assert "item-clickable" in r.text
    # The row still contains the real title <a> — that's what the
    # stretched hit area anchors to.
    assert 'class="item-title"' in r.text


def test_activity_page_marks_rows_with_url_clickable():
    """Creating a category emits an activity row with no subject_url
    (it's site-internal). Bookmarking emits one WITH a URL. Only the
    latter should be clickable."""
    _, item_id = _seed_feed_and_item()
    client = _client()
    _login(client, "alice@example.com")
    client.post("/categories", data={"name": "News"})  # url-less activity
    client.post(f"/items/{item_id}/bookmark")           # url'd activity

    r = client.get("/u/alice/activity")
    assert r.status_code == 200
    # At least one row got the class (the bookmark event)…
    assert "item-clickable" in r.text
    # …and the category-creation event's URL is empty, so the row's
    # <li> doesn't carry item-clickable. Verify the specific fragment.
    assert 'class="fav-item"' in r.text  # non-clickable row exists too


def test_friends_feed_marks_rows_with_url_clickable():
    """The /feed page shows what people you follow have done. Rows
    for activity with a subject_url become clickable."""
    _, item_id = _seed_feed_and_item()

    # Owner bookmarks something so there's a url'd activity row.
    owner = _client()
    _login(owner, "owner@example.com")
    owner.post(f"/items/{item_id}/bookmark")

    # Viewer follows owner and loads /feed.
    viewer = _client()
    _login(viewer, "viewer@example.com")
    viewer.post("/u/owner/follow")

    r = viewer.get("/feed")
    assert r.status_code == 200
    assert "item-clickable" in r.text
