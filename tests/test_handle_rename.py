"""Account rename (0.9.0) — POST /settings/handle + alias redirects.

Rename semantics:
  - Old handle permanently reserved in HandleAlias (nobody can claim it,
    even the original owner via a future rename).
  - GETs to /u/{old}/... 301 to /u/{new}/... preserving path + query.
  - POSTs to /u/{old}/... serve normally under the alias (no redirect —
    some HTTP clients don't preserve method across 301).
  - Denormalized handle refs (Activity.subject, subject_url, and
    category-follow Feed.url) rewrite atomically.
  - Rate-limited to one rename per 30 days per user.
"""


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


def _rename(client, new_handle, follow=False):
    return client.post(
        "/settings/handle",
        data={"new_handle": new_handle},
        follow_redirects=follow,
    )


# ---- Happy path --------------------------------------------------------


def test_rename_updates_user_and_creates_alias():
    """Basic rename: User.handle changes, HandleAlias row created,
    303 redirect to the new radar."""
    import app.db as db_mod
    c = _client()
    _login(c, "alice@example.com")

    r = _rename(c, "alice-cooper")
    assert r.status_code == 303
    assert r.headers["location"] == "/u/alice-cooper"

    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.email == "alice@example.com")).one()
        assert user.handle == "alice-cooper"
        assert user.last_renamed_at is not None
        alias = s.exec(
            select(db_mod.HandleAlias).where(db_mod.HandleAlias.old_handle == "alice")
        ).first()
        assert alias is not None
        assert alias.user_id == user.id


def test_rename_no_op_returns_early_without_creating_alias():
    """Renaming to your current handle is a benign no-op — no alias
    row, no last_renamed_at stamp."""
    import app.db as db_mod
    c = _client()
    _login(c, "alice@example.com")

    r = _rename(c, "alice")
    assert r.status_code == 303
    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.email == "alice@example.com")).one()
        assert user.handle == "alice"
        assert user.last_renamed_at is None
        assert s.exec(select(db_mod.HandleAlias)).first() is None


# ---- Redirects on old-handle URLs -------------------------------------


def test_get_old_handle_url_301s_to_current():
    c = _client()
    _login(c, "alice@example.com")
    _rename(c, "alice-cooper", follow=True)

    r = _client().get("/u/alice", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/u/alice-cooper"


def test_get_old_handle_subpath_preserves_path_and_query():
    """/u/{old}/bookmarks?view=stream → /u/{new}/bookmarks?view=stream."""
    c = _client()
    _login(c, "alice@example.com")
    _rename(c, "alice-cooper", follow=True)

    for path in (
        "/u/alice/bookmarks",
        "/u/alice/activity",
        "/u/alice.rss",
        "/u/alice.opml",
        "/u/alice/followers",
        "/u/alice/following",
    ):
        r = _client().get(path, follow_redirects=False)
        assert r.status_code == 301, path
        assert r.headers["location"] == path.replace("alice", "alice-cooper", 1), path

    # Query string carried through.
    r = _client().get(
        "/u/alice?cat=News&view=stream", follow_redirects=False,
    )
    assert r.status_code == 301
    assert r.headers["location"] == "/u/alice-cooper?cat=News&view=stream"


def test_chain_of_renames_alias_points_at_current():
    """alice → bob → carol: both /u/alice and /u/bob 301 to /u/carol."""
    c = _client()
    _login(c, "alice@example.com")
    _rename(c, "bob", follow=True)

    # Skip the cooldown to test the chain quickly.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        u = s.exec(select(db_mod.User).where(db_mod.User.email == "alice@example.com")).one()
        u.last_renamed_at = None
        s.add(u)
        s.commit()

    _rename(c, "carol", follow=True)

    r1 = _client().get("/u/alice", follow_redirects=False)
    r2 = _client().get("/u/bob", follow_redirects=False)
    assert r1.status_code == 301 and r1.headers["location"] == "/u/carol"
    assert r2.status_code == 301 and r2.headers["location"] == "/u/carol"


def test_post_to_old_handle_serves_normally_without_redirect():
    """POST /u/{old}/follow doesn't 301 — it resolves the alias and
    creates the follow under the current user."""
    owner = _client()
    _login(owner, "owner@example.com")
    _rename(owner, "owner-renamed", follow=True)

    viewer = _client()
    _login(viewer, "viewer@example.com")
    r = viewer.post("/u/owner/follow", follow_redirects=False)
    assert r.status_code == 200  # HTMX button partial

    import app.db as db_mod
    with Session(db_mod.engine) as s:
        u = s.exec(select(db_mod.User).where(db_mod.User.handle == "owner-renamed")).one()
        v = s.exec(select(db_mod.User).where(db_mod.User.email == "viewer@example.com")).one()
        row = s.get(db_mod.Follow, (v.id, u.id))
        assert row is not None


# ---- Validation gates --------------------------------------------------


def test_rename_shape_invalid_400():
    c = _client()
    _login(c, "alice@example.com")
    for bad in ("A", "ab", "-abc", "1abc", "abc!", "toolong" * 10):
        r = _rename(c, bad)
        assert r.status_code == 400, bad


def test_rename_reserved_name_409():
    c = _client()
    _login(c, "alice@example.com")
    for reserved in ("dashboard", "help", "discover", "login", "settings"):
        r = _rename(c, reserved)
        assert r.status_code == 409, reserved


def test_rename_live_collision_409():
    a = _client()
    _login(a, "alice@e.com")
    b = _client()
    _login(b, "bob-taken@e.com")
    r = _rename(a, "bob-taken")
    assert r.status_code == 409


def test_rename_alias_collision_409():
    """Someone else's retired handle is permanently reserved."""
    a = _client()
    _login(a, "alice@e.com")
    _rename(a, "alice-renamed", follow=True)

    b = _client()
    _login(b, "bob@e.com")
    r = _rename(b, "alice")  # 'alice' is now an alias
    assert r.status_code == 409


def test_rename_own_prior_alias_409():
    """User can't rename back to their own retired handle — chain of
    aliases stays clean, no ping-pong."""
    c = _client()
    _login(c, "alice@example.com")
    _rename(c, "bob", follow=True)
    # Skip cooldown for the test.
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        u = s.exec(select(db_mod.User).where(db_mod.User.email == "alice@example.com")).one()
        u.last_renamed_at = None
        s.add(u)
        s.commit()

    r = _rename(c, "alice")
    assert r.status_code == 409


def test_rename_cooldown_429():
    """Second rename within 30 days is rejected with 429."""
    c = _client()
    _login(c, "alice@example.com")
    r1 = _rename(c, "alice-two")
    assert r1.status_code == 303
    r2 = _rename(c, "alice-three")
    assert r2.status_code == 429


# ---- Denormalized rewrites --------------------------------------------


def test_rename_rewrites_own_activity_subject_url():
    """Activity rows the user created — e.g. `created_category News` —
    have subject_urls that may point at /u/{old}/… (though most don't).
    Verifying via a `followed` row that DOES carry /u/{target}."""
    import app.db as db_mod
    # Set up: alice follows bob. alice's activity row has:
    #   subject='@bob', subject_url='/u/bob'
    a = _client()
    _login(a, "a@e.com")
    b = _client()
    _login(b, "b@e.com")
    a.post("/u/b/follow")

    # Now bob renames.
    _rename(b, "bob-new", follow=True)

    with Session(db_mod.engine) as s:
        alice = s.exec(select(db_mod.User).where(db_mod.User.email == "a@e.com")).one()
        row = s.exec(
            select(db_mod.Activity)
            .where(db_mod.Activity.user_id == alice.id)
            .where(db_mod.Activity.kind == "followed")
        ).one()
        assert row.subject == "@bob-new"
        assert row.subject_url == "/u/bob-new"


def test_rename_rewrites_category_follow_feed_url():
    """A CategoryFollow-created Feed.url embedding /u/{owner}/c/{cat}.rss
    must be rewritten so refresh cycles keep working post-rename."""
    import app.db as db_mod
    owner = _client()
    _login(owner, "owner@e.com")
    owner.post("/categories", data={"name": "News"})

    # Manually create a category-follow-shaped feed row.
    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://rssfriends.com/u/owner/c/News.rss",
            title="@owner's News",
        )
        s.add(feed)
        s.commit()

    _rename(owner, "owner-new", follow=True)

    with Session(db_mod.engine) as s:
        feed = s.exec(select(db_mod.Feed)).one()
        assert feed.url == "https://rssfriends.com/u/owner-new/c/News.rss"


# ---- New-user signup collision -----------------------------------------


def test_new_signup_avoids_retired_alias():
    """Signup helper _unique_handle must skip retired aliases when
    autogenerating a handle for a new email."""
    a = _client()
    _login(a, "a@e.com")  # handle: "a"
    _rename(a, "a-cooper", follow=True)  # "a" is now a retired alias

    # New user with an email that would otherwise slugify to "a" —
    # picks the next free integer suffix instead.
    b = _client()
    _login(b, "a@other-domain.example")
    import app.db as db_mod
    with Session(db_mod.engine) as s:
        u = s.exec(select(db_mod.User).where(db_mod.User.email == "a@other-domain.example")).one()
        assert u.handle != "a"
        assert u.handle.startswith("a-")
