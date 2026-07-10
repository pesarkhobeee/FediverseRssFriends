"""OPML import: parser + endpoint."""

import io

from fastapi.testclient import TestClient


def _client():
    import app.main
    return TestClient(app.main.app)


def _login(client, email):
    r = client.post("/login", data={"email": email})
    marker = "/auth/verify?token="
    idx = r.text.find(marker)
    end = r.text.find('"', idx)
    client.get(r.text[idx:end], follow_redirects=False)


SAMPLE_OPML = b"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>my reading list</title></head>
  <body>
    <outline text="Weblogs">
      <outline type="rss" text="Proton Blog" xmlUrl="https://proton.me/blog/feed" htmlUrl="https://proton.me/blog"/>
      <outline type="rss" text="Rustacean" xmlUrl="https://blog.rust-lang.org/feed.xml"/>
    </outline>
    <outline text="Comics">
      <outline type="rss" text="XKCD" xmlUrl="https://xkcd.com/atom.xml"/>
    </outline>
    <outline type="rss" text="Flat one" xmlUrl="https://flat.example/feed"/>
  </body>
</opml>
"""

DUP_OPML = b"""<?xml version="1.0"?>
<opml><body>
  <outline text="A"><outline type="rss" xmlUrl="https://dup.example/feed"/></outline>
  <outline text="B"><outline type="rss" xmlUrl="https://dup.example/feed"/></outline>
</body></opml>
"""


def test_parse_opml_nested_and_flat():
    from app.opml import parse_opml

    entries = parse_opml(SAMPLE_OPML)
    urls = [e.xml_url for e in entries]
    assert "https://proton.me/blog/feed" in urls
    assert "https://xkcd.com/atom.xml" in urls
    assert "https://flat.example/feed" in urls
    proton = next(e for e in entries if "proton" in e.xml_url)
    assert proton.category == "Weblogs"
    assert proton.title == "Proton Blog"
    flat = next(e for e in entries if "flat.example" in e.xml_url)
    assert flat.category == "Imported"


def test_parse_opml_dedups_within_file():
    from app.opml import parse_opml

    entries = parse_opml(DUP_OPML)
    assert len(entries) == 1
    assert entries[0].category == "A"  # first-wins


def test_parse_opml_rejects_junk():
    import pytest

    from app.opml import parse_opml

    with pytest.raises(ValueError):
        parse_opml(b"not xml <<<")


def test_import_opml_creates_everything():
    client = _client()
    _login(client, "farid@example.com")

    r = client.post(
        "/import/opml",
        files={"file": ("radar.opml", io.BytesIO(SAMPLE_OPML), "text/xml")},
    )
    assert r.status_code == 200
    assert "Imported <strong>4</strong> new feed" in r.text

    from sqlmodel import Session, select

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "farid")).one()
        cats = {
            c.name for c in s.exec(
                select(db_mod.Category).where(db_mod.Category.user_id == user.id)
            ).all()
        }
        subs = s.exec(
            select(db_mod.Subscription).where(db_mod.Subscription.user_id == user.id)
        ).all()
    assert cats == {"Weblogs", "Comics", "Imported"}
    assert len(subs) == 4


def test_import_opml_second_time_is_all_duplicates():
    client = _client()
    _login(client, "farid@example.com")
    client.post(
        "/import/opml",
        files={"file": ("r.opml", io.BytesIO(SAMPLE_OPML), "text/xml")},
    )
    r = client.post("/import/opml", files={"file": ("r.opml", io.BytesIO(SAMPLE_OPML), "text/xml")})
    assert r.status_code == 200
    assert "Imported <strong>0</strong>" in r.text
    assert "Skipped <strong>4</strong>" in r.text


def test_import_opml_merges_new_feeds_into_existing_category(monkeypatch):
    """A user's manually-created 'Weblogs' should be reused, not duplicated."""
    import app.dashboard

    def fake_resolve(url):
        return {"title": "manual", "site_url": "", "favicon_url": ""}

    monkeypatch.setattr(app.dashboard, "resolve_feed", fake_resolve)

    client = _client()
    _login(client, "u@example.com")
    # Create a Weblogs category manually with one feed.
    r_cat = client.post("/categories", data={"name": "Weblogs"})
    idx = r_cat.text.find('id="category-') + len('id="category-')
    end = r_cat.text.find('"', idx)
    cat_id = int(r_cat.text[idx:end])
    client.post("/feeds", data={"url": "https://manual.example/feed", "category_id": cat_id})

    # Now import OPML which also has a "Weblogs" category.
    r = client.post("/import/opml", files={"file": ("r.opml", io.BytesIO(SAMPLE_OPML), "text/xml")})
    assert r.status_code == 200

    from sqlmodel import Session, select

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "u")).one()
        cats = s.exec(select(db_mod.Category).where(db_mod.Category.user_id == user.id)).all()
    names_lower = [c.name.lower() for c in cats]
    assert names_lower.count("weblogs") == 1  # not duplicated


def test_import_opml_empty_file_rejected():
    client = _client()
    _login(client, "u@example.com")
    r = client.post("/import/opml", files={"file": ("r.opml", io.BytesIO(b""), "text/xml")})
    assert r.status_code == 400


def test_import_opml_requires_login():
    r = _client().post(
        "/import/opml",
        files={"file": ("r.opml", io.BytesIO(SAMPLE_OPML), "text/xml")},
    )
    assert r.status_code == 401
