from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


def _now() -> datetime:
    return datetime.now(UTC)


def _rand_secret() -> str:
    import secrets
    return secrets.token_urlsafe(24)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    handle: str = Field(index=True, unique=True)
    visibility: str = Field(default="public")  # public | unlisted | private
    # Per-user secret mixed into activity.rss signed tokens. Rotating it
    # invalidates every previously-shared feed link.
    activity_secret: str = Field(default_factory=_rand_secret)
    created_at: datetime = Field(default_factory=_now)
    # 0.9.0: handle-rename rate limit anchor. NULL = never renamed.
    # Users may rename at most once every RENAME_COOLDOWN_DAYS.
    last_renamed_at: datetime | None = Field(default=None)


class HandleAlias(SQLModel, table=True):
    """A retired handle. Created on every successful account rename.

    Semantics (0.9.0):
      - The old handle is permanently reserved — nobody else can claim
        it, and the original owner can't rename BACK to it either
        (prevents ping-pong renames that could confuse redirects).
      - Every GET request to /u/{old_handle}/… 301-redirects to
        /u/{current_handle}/…, so external RSS subscribers, followers'
        bookmarks, and old shared links keep working forever.
      - POSTs (follow/unfollow/fork) are served under the alias without
        redirect — a 301 wouldn't preserve HTTP method on all clients.

    See app/public.py::resolve_handle for the lookup path."""

    old_handle: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    renamed_at: datetime = Field(default_factory=_now)


class MagicToken(SQLModel, table=True):
    token: str = Field(primary_key=True)
    email: str = Field(index=True)
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime


class Category(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    name: str
    sort_order: int = Field(default=0)


class Feed(SQLModel, table=True):
    """Global row, deduplicated by URL — every subscribed URL exists once."""

    id: int | None = Field(default=None, primary_key=True)
    url: str = Field(unique=True, index=True)
    title: str
    site_url: str = Field(default="")
    favicon_url: str = Field(default="")
    last_fetched_at: datetime | None = Field(default=None)
    consecutive_failures: int = Field(default=0)
    last_error: str = Field(default="")


class Subscription(SQLModel, table=True):
    """A user's assignment of a Feed to one of their Categories."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    feed_id: int = Field(foreign_key="feed.id", index=True)
    category_id: int = Field(foreign_key="category.id", index=True)
    sort_order: int = Field(default=0)
    # 0.11.2: private per-user note. Shown as a subtitle under the feed
    # title on the reader's timeline/bookmarks/activity. Never rendered
    # publicly (RSS, other users' /u/{handle}, etc.). Purpose: help the
    # user remember who a cryptic handle actually is ("DHH → Rails
    # creator", "the_pythonist → @Samuel").
    note: str = Field(default="", max_length=280,
                      sa_column_kwargs={"server_default": ""})


class FeedItem(SQLModel, table=True):
    """One entry parsed from a Feed. Dedup key is (feed_id, guid)."""

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id", index=True)
    guid: str = Field(index=True)
    title: str
    link: str = Field(default="")
    summary: str = Field(default="")
    published_at: datetime | None = Field(default=None, index=True)
    fetched_at: datetime = Field(default_factory=_now)


class Bookmark(SQLModel, table=True):
    """A user marking a FeedItem as saved. Composite PK deduplicates.

    History: this was 'Favorite' up to 0.2.35 (renamed 0.2.36 to soften
    the verb — bookmarks are "I saved this" rather than "I loved this").
    init_db renames the old `favorite` table to `bookmark` and rewrites
    Activity.kind='favorited' → 'bookmarked' on startup."""

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    feed_item_id: int = Field(foreign_key="feeditem.id", primary_key=True)
    created_at: datetime = Field(default_factory=_now, index=True)


class Follow(SQLModel, table=True):
    """A directed follow. approved_at is None → pending request (private
    followees only); NOT NULL → approved (implicit for public/unlisted)."""

    follower_id: int = Field(foreign_key="user.id", primary_key=True)
    followee_id: int = Field(foreign_key="user.id", primary_key=True, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    approved_at: datetime | None = Field(default=None, index=True)


class Activity(SQLModel, table=True):
    """Append-only log of user actions. Subject fields are denormalized so
    the timeline renders correctly even after the referenced row is deleted."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # kinds: subscribed | favorited | followed | forked | created_category | posted
    kind: str = Field(index=True)
    subject: str = Field(default="")
    subject_url: str = Field(default="")
    ref_type: str = Field(default="")
    ref_id: int | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)


class CategoryFollow(SQLModel, table=True):
    """A directed follow scoped to a single category on someone else's
    radar — a finer-grained alternative to whole-user Follow. Only
    item-scoped activity (subscribed/favorited) whose feed lives in
    this category surfaces on the follower's timeline via this row.
    Person-scoped activity (followed/forked/created_category/posted)
    only reaches followers via a whole-user Follow."""

    __table_args__ = (
        UniqueConstraint("follower_id", "category_id", name="uq_catfollow_follower_cat"),
    )

    id: int | None = Field(default=None, primary_key=True)
    follower_id: int = Field(foreign_key="user.id", index=True)
    category_id: int = Field(foreign_key="category.id", index=True)
    created_at: datetime = Field(default_factory=_now, index=True)


class Collection(SQLModel, table=True):
    """Community-curated collection of feeds around a topic + place + language.

    Started 0.10.0 to solve the RSS-discovery-is-all-English/BBC-CNN
    problem. First collection: 'Berlin — Nachrichten' with 12 verified
    German + English Berlin feeds. See docs/collections.md.

    `curator_id` NULL = system-curated (moderated by whoever `is_admin()`
    returns true for). Non-null = curated by that user; they moderate.
    `is_official` = shown on /discover's Browse collections section.
    `is_public` = anyone can visit (drafts stay hidden)."""

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    name: str
    description: str = Field(default="")
    language: str = Field(default="")  # ISO 639-1: 'de', 'en', 'fa'
    locale: str = Field(default="")    # freeform 'Berlin, DE'
    curator_id: int | None = Field(default=None, foreign_key="user.id", index=True)
    is_official: bool = Field(default=False)
    is_public: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_now)


class CollectionFeed(SQLModel, table=True):
    """A Feed inside a Collection. `note` is the curator's one-line
    aside ('Öffentlich-rechtlich, seriös' etc). Reuses the global Feed
    row so subscribers who add this feed and users on the collection
    page share the same Feed identity."""

    __table_args__ = (
        UniqueConstraint("collection_id", "feed_id", name="uq_collectionfeed_pair"),
    )

    id: int | None = Field(default=None, primary_key=True)
    collection_id: int = Field(foreign_key="collection.id", index=True)
    feed_id: int = Field(foreign_key="feed.id", index=True)
    note: str = Field(default="")
    sort_order: int = Field(default=0)


class CollectionFeedTombstone(SQLModel, table=True):
    """0.11.4: records a curator's explicit removal of a feed URL from a
    Collection. The seed loop (which is idempotent-per-feed and would
    otherwise happily re-add anything in the seed dict) consults this
    table and skips any URL that has been tombstoned. If a curator
    later re-adds the same URL via the UI, the tombstone is cleared so
    future seed passes are once again free to keep it in sync.

    Why URL and not feed_id: the Feed row can be deleted (or the same
    URL can point at a different Feed after re-resolution), but the
    intent 'I don't want this URL in this collection' is stable across
    those churn events."""

    __table_args__ = (
        UniqueConstraint("collection_id", "feed_url",
                         name="uq_collectionfeedtombstone_pair"),
    )

    id: int | None = Field(default=None, primary_key=True)
    collection_id: int = Field(foreign_key="collection.id", index=True)
    feed_url: str
    removed_by: int | None = Field(default=None, foreign_key="user.id")
    removed_at: datetime = Field(default_factory=_now)


class CollectionRequest(SQLModel, table=True):
    """0.11.4: a request from a signed-in user for a whole new
    Collection to exist (as distinct from CollectionSuggestion, which
    adds one feed to an *existing* collection). No auto-creation —
    admins review, create the Collection via the admin UI, and mark
    the request accepted."""

    id: int | None = Field(default=None, primary_key=True)
    requested_by: int = Field(foreign_key="user.id", index=True)
    name: str  # e.g. "Amsterdam — Nieuws"
    description: str = Field(default="")
    language: str = Field(default="")   # ISO 639-1
    locale: str = Field(default="")     # freeform
    example_feeds: str = Field(default="")  # newline-separated hints
    status: str = Field(default="pending")  # pending | accepted | rejected
    reviewed_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class CollectionSuggestion(SQLModel, table=True):
    """A pending suggestion from a user to add a feed to a collection.
    Never auto-published — the curator (or admin for system-curated
    collections) reviews and accepts or rejects. `suggested_by` is
    NULL for anonymous suggestions (rate-limited by IP at the route)."""

    id: int | None = Field(default=None, primary_key=True)
    collection_id: int = Field(foreign_key="collection.id", index=True)
    feed_url: str
    note: str = Field(default="")
    suggested_by: int | None = Field(default=None, foreign_key="user.id")
    status: str = Field(default="pending")  # pending | accepted | rejected
    reviewed_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class WallabagAccount(SQLModel, table=True):
    """A user's Wallabag OAuth link. One row per user max.

    Tokens + client_secret are stored plaintext — same posture as
    SESSION_SECRET and User.activity_secret. An attacker with the DB
    file can also read the process env, so at-rest encryption with a
    key stored alongside doesn't raise the bar meaningfully.

    `enabled` gates the bookmark hook — flipping it off lets the user
    keep their creds but pause sending."""

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_wallabag_user"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    instance_url: str  # e.g. https://app.wallabag.it (no trailing slash)
    client_id: str
    client_secret: str
    # Wallabag's public OAuth API only supports grant_type=password
    # (see https://doc.wallabag.org/developer/api/oauth/) — there is no
    # /oauth/v2/auth authorization-code endpoint. So we store the user's
    # Wallabag username + password and exchange them for tokens directly.
    username: str = Field(default="")
    password: str = Field(default="")
    access_token: str = Field(default="")
    refresh_token: str = Field(default="")
    token_expires_at: datetime | None = Field(default=None)
    enabled: bool = Field(default=False)  # True after successful OAuth
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class PersonalFeed(SQLModel, table=True):
    """A feed the user claims as their own — new items from it emit
    Activity(kind='posted') for that user, so followers see the cross-
    post on their timeline. Different from Subscription (which means
    "I read this"); a personal feed means "I authored this"."""

    __table_args__ = (
        UniqueConstraint("user_id", "feed_id", name="uq_personal_user_feed"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    feed_id: int = Field(foreign_key="feed.id", index=True)
    label: str = Field(default="")
    # `connected_at` is the point-in-time cutoff — only feed items
    # published/fetched AFTER this instant emit Activity for this user.
    # Prevents historical dumps flooding followers' timelines on connect.
    connected_at: datetime = Field(default_factory=_now)


def _sqlite_column_exists(conn, table: str, column: str) -> bool:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _run_sqlite_migrations(conn) -> None:
    """Idempotent, ALTER-TABLE-based migrations for SQLite. Runs on every
    startup and is a no-op after the first successful run."""
    import secrets

    # 0.9.0: User.last_renamed_at added for the rename rate-limit anchor.
    # Nullable and defaults to NULL (users who never renamed).
    if _sqlite_column_exists(conn, "user", "id") and not _sqlite_column_exists(
        conn, "user", "last_renamed_at"
    ):
        conn.exec_driver_sql(
            "ALTER TABLE user ADD COLUMN last_renamed_at TIMESTAMP"
        )

    # activity_secret was added after the initial schema — backfill it.
    if _sqlite_column_exists(conn, "user", "id") and not _sqlite_column_exists(
        conn, "user", "activity_secret"
    ):
        conn.exec_driver_sql(
            "ALTER TABLE user ADD COLUMN activity_secret TEXT NOT NULL DEFAULT ''"
        )
        rows = conn.exec_driver_sql(
            "SELECT id FROM user WHERE activity_secret = ''"
        ).fetchall()
        for (uid,) in rows:
            conn.exec_driver_sql(
                "UPDATE user SET activity_secret = ? WHERE id = ?",
                (secrets.token_urlsafe(24), uid),
            )

    # Backfill Mastodon-style "(untitled)" rows that DO have a summary.
    # One-shot: after the first pass rows change so subsequent starts
    # scan zero rows.
    if _sqlite_column_exists(conn, "feeditem", "title"):
        _backfill_untitled_items(conn)

    # Backfill GitHub owner avatars on existing Feed rows so users don't
    # have to wait for the next refresh cycle to see real profile pics.
    if _sqlite_column_exists(conn, "feed", "favicon_url"):
        _backfill_github_avatars(conn)

    # Normalize legacy GitHub/Reddit URLs that were stored without their
    # .atom / .rss suffix (a common shape in exported OPML).
    if _sqlite_column_exists(conn, "feed", "url"):
        _backfill_feed_urls(conn)

    # Rewrite generic GitHub feed titles ("Github Public timeline feed")
    # to the username, matching how lovelyRSS displays them.
    if _sqlite_column_exists(conn, "feed", "title"):
        _backfill_github_titles(conn)

    # 0.2.36: Favorite → Bookmark rename. Rename the table and rewrite
    # Activity.kind='favorited' → 'bookmarked'. Idempotent — after the
    # first successful pass both branches are no-ops.
    #
    # Note on ordering: SQLModel.create_all runs BEFORE migrations, so
    # on the first post-rename boot BOTH `favorite` (existing) and
    # `bookmark` (auto-created empty) tables exist. Detect that case
    # and swap them; otherwise the old rows would be orphaned.
    tables = {
        row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "favorite" in tables:
        old_rows = conn.exec_driver_sql("SELECT COUNT(*) FROM favorite").fetchone()[0]
        if "bookmark" in tables:
            new_rows = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM bookmark"
            ).fetchone()[0]
            # Only swap if the auto-created bookmark table is empty
            # (first boot after rename); if bookmark has data, it means
            # a later boot has already migrated and both tables happen
            # to coexist — do nothing.
            if new_rows == 0 and old_rows > 0:
                conn.exec_driver_sql("DROP TABLE bookmark")
                conn.exec_driver_sql("ALTER TABLE favorite RENAME TO bookmark")
            elif old_rows == 0:
                # favorite is a stale empty table left after migration — drop it.
                conn.exec_driver_sql("DROP TABLE favorite")
        else:
            conn.exec_driver_sql("ALTER TABLE favorite RENAME TO bookmark")
    if "activity" in tables:
        conn.exec_driver_sql(
            "UPDATE activity SET kind = 'bookmarked' WHERE kind = 'favorited'"
        )

    # 0.3.1: WallabagAccount grew `username` + `password` columns when
    # we discovered Wallabag only supports the password grant. Any 0.3.0
    # rows are unusable anyway (the auth flow was broken) so the
    # columns can start empty.
    if "wallabagaccount" in tables:
        for col in ("username", "password"):
            if not _sqlite_column_exists(conn, "wallabagaccount", col):
                conn.exec_driver_sql(
                    f"ALTER TABLE wallabagaccount ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )

    # 0.11.4 -> 0.11.9: the tombstone backfill migration that USED to
    # live here was buggy. It ran on every startup (not once), and its
    # heuristic — "URL in seed dict but not in DB → tombstone it" —
    # can't tell "curator removed this before the feature landed" from
    # "developer just added a new seed URL." On the 0.11.9 deploy it
    # silently tombstoned the 9 new top-gun URLs I'd just added to
    # engineer-weblogs, so they never seeded. Removed entirely — the
    # tehran-farsi case it was written for has been fixed by the
    # tombstones already recorded during its first successful run.
    # Future curator removals go through the normal UI path which
    # writes tombstones directly.

    # 0.11.2: Subscription.note added for private per-user reminder notes
    # ("DHH → Rails creator"). Backfilled to empty string.
    if _sqlite_column_exists(conn, "subscription", "id") and not _sqlite_column_exists(
        conn, "subscription", "note"
    ):
        conn.exec_driver_sql(
            "ALTER TABLE subscription ADD COLUMN note TEXT NOT NULL DEFAULT ''"
        )

    # 0.2.43: category follows started auto-adding a Subscription for
    # the category's RSS URL. We used to backfill this on every startup
    # — but that means if a user removes the auto-added sub from their
    # dashboard, the next deploy silently puts it back (bug reported
    # by @julian-lamas 2026-08-02). Since new CategoryFollow rows are
    # created with a matching Subscription atomically by follow.py's
    # _ensure_category_subscription(), no backfill is needed anymore.
    #
    # The function is kept in this file for reference + tests, but it
    # no longer runs on startup. If you ever need to re-heal a specific
    # historical drift, call it manually from a one-shot admin script.


def _backfill_category_follow_subscriptions(conn) -> None:
    """For each CategoryFollow row, ensure follower has a Subscription
    to that category's RSS URL under a singleton 'Following' category
    on their own radar. Runs once (idempotent — reruns just skip
    already-linked rows)."""
    import os

    base = (os.environ.get("RSSFRIENDS_BASE_URL") or "https://rssfriends.com").rstrip("/")
    rows = conn.exec_driver_sql(
        "SELECT cf.follower_id, cat.id, cat.name, cat.user_id, u.handle "
        "FROM categoryfollow cf "
        "JOIN category cat ON cat.id = cf.category_id "
        "JOIN user u ON u.id = cat.user_id"
    ).fetchall()
    for follower_id, _cat_id, cat_name, _owner_id, owner_handle in rows:
        rss_url = f"{base}/u/{owner_handle}/c/{cat_name}.rss"
        # find-or-create Feed
        row = conn.exec_driver_sql(
            "SELECT id FROM feed WHERE url = ?", (rss_url,)
        ).fetchone()
        if row:
            feed_id = row[0]
        else:
            conn.exec_driver_sql(
                "INSERT INTO feed (url, title, site_url, favicon_url, "
                "consecutive_failures, last_error) "
                "VALUES (?, ?, ?, '', 0, '')",
                (rss_url, f"@{owner_handle}'s {cat_name}",
                 f"{base}/u/{owner_handle}?cat={cat_name}"),
            )
            feed_id = conn.exec_driver_sql(
                "SELECT id FROM feed WHERE url = ?", (rss_url,)
            ).fetchone()[0]
        # find-or-create "Following" category on follower's radar
        row = conn.exec_driver_sql(
            "SELECT id FROM category WHERE user_id = ? AND lower(name) = 'following'",
            (follower_id,),
        ).fetchone()
        if row:
            following_cat_id = row[0]
        else:
            max_order = conn.exec_driver_sql(
                "SELECT COALESCE(MAX(sort_order), 0) FROM category WHERE user_id = ?",
                (follower_id,),
            ).fetchone()[0]
            conn.exec_driver_sql(
                "INSERT INTO category (user_id, name, sort_order) VALUES (?, 'Following', ?)",
                (follower_id, max_order + 1),
            )
            following_cat_id = conn.exec_driver_sql(
                "SELECT id FROM category WHERE user_id = ? AND lower(name) = 'following'",
                (follower_id,),
            ).fetchone()[0]
        # skip if already subscribed
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM subscription WHERE user_id = ? AND feed_id = ?",
            (follower_id, feed_id),
        ).fetchone()
        if exists:
            continue
        max_sub_order = conn.exec_driver_sql(
            "SELECT COALESCE(MAX(sort_order), 0) FROM subscription "
            "WHERE user_id = ? AND category_id = ?",
            (follower_id, following_cat_id),
        ).fetchone()[0]
        conn.exec_driver_sql(
            "INSERT INTO subscription (user_id, feed_id, category_id, sort_order) "
            "VALUES (?, ?, ?, ?)",
            (follower_id, feed_id, following_cat_id, max_sub_order + 1),
        )


def _backfill_github_avatars(conn) -> None:
    from .feeds import github_owner

    rows = conn.exec_driver_sql(
        "SELECT id, url, favicon_url FROM feed WHERE url LIKE '%github.com%'"
    ).fetchall()
    for row_id, url, favicon_url in rows:
        owner = github_owner(url)
        if not owner:
            continue
        want = f"https://avatars.githubusercontent.com/{owner}?s=64"
        # Also rewrite the OLD github.com/{owner}.png form to the CDN URL
        # (as of 0.2.29 — github.com now 403s the .png shortcut).
        old = f"https://github.com/{owner}.png?size=64"
        if favicon_url == old:
            favicon_url = None  # force the update below
        if favicon_url == want:
            continue
        conn.exec_driver_sql(
            "UPDATE feed SET favicon_url = ? WHERE id = ?", (want, row_id)
        )


def _backfill_feed_urls(conn) -> None:
    """One-shot: rewrite `github.com/user` → `github.com/user.atom` and
    `reddit.com/r/x/` → `reddit.com/r/x/.rss` for Feed rows that were
    imported before URL normalization existed.

    If the normalized URL already exists (a duplicate Feed row), migrate
    every Subscription that pointed at the old row to the new one, then
    delete the old row. Otherwise just UPDATE the URL in place."""
    from .feeds import normalize_feed_url

    rows = conn.exec_driver_sql("SELECT id, url FROM feed").fetchall()
    for row_id, url in rows:
        new = normalize_feed_url(url)
        if new == url:
            continue
        existing = conn.exec_driver_sql(
            "SELECT id FROM feed WHERE url = ?", (new,)
        ).fetchone()
        if existing:
            new_id = existing[0]
            # Move every subscription over, dedup'd by (user, feed).
            conn.exec_driver_sql(
                "UPDATE OR IGNORE subscription SET feed_id = ? WHERE feed_id = ?",
                (new_id, row_id),
            )
            # Remaining subs that hit the UNIQUE constraint stay pointing
            # at the old row and would block deletion — kill them.
            conn.exec_driver_sql(
                "DELETE FROM subscription WHERE feed_id = ?", (row_id,)
            )
            conn.exec_driver_sql("DELETE FROM feed WHERE id = ?", (row_id,))
        else:
            conn.exec_driver_sql(
                "UPDATE feed SET url = ?, consecutive_failures = 0, "
                "last_error = '' WHERE id = ?",
                (new, row_id),
            )


def _backfill_github_titles(conn) -> None:
    """GitHub user/org feeds all ship a generic title upstream. Rewrite to
    the username so users see 'schacon' instead of 'Github Public timeline
    feed'. Idempotent — after the first pass no rows match."""
    from .feeds import github_owner

    rows = conn.exec_driver_sql(
        "SELECT id, url, title FROM feed WHERE url LIKE '%github.com%'"
    ).fetchall()
    for row_id, url, title in rows:
        owner = github_owner(url)
        if not owner or title == owner:
            continue
        conn.exec_driver_sql(
            "UPDATE feed SET title = ? WHERE id = ?", (owner, row_id)
        )


def _backfill_untitled_items(conn) -> None:
    import re as _re
    tag = _re.compile(r"<[^>]+>")
    ws = _re.compile(r"\s+")
    punct = _re.compile(r"\s+([.,!?;:])")
    rows = conn.exec_driver_sql(
        "SELECT id, summary FROM feeditem "
        "WHERE title = '(untitled)' AND summary != '' LIMIT 5000"
    ).fetchall()
    for row_id, summary in rows:
        text = ws.sub(" ", tag.sub(" ", summary or "")).strip()
        text = punct.sub(r"\1", text)
        if not text:
            continue
        title = (text[:120] + "…") if len(text) > 120 else text
        conn.exec_driver_sql(
            "UPDATE feeditem SET title = ? WHERE id = ?", (title, row_id)
        )


def init_db() -> None:
    # SQLite: WAL for better concurrency under a single process.
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    SQLModel.metadata.create_all(engine)
    if DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            _run_sqlite_migrations(conn)
    _seed_official_collections()


# ─── 0.10.0: seed the first official community-curated collection ──────
# Runs on every startup but is idempotent — creates rows only when the
# slug is missing. See docs/collections.md for the philosophy.

SEED_BERLIN_NACHRICHTEN = {
    "slug": "berlin-nachrichten",
    "name": "Berlin — Nachrichten",
    "description": (
        "Deutsch- und englischsprachige RSS-Feeds über Berlin. "
        "Öffentlich-rechtlich, klassische Presse, alternative und "
        "hyperlokale Quellen — hand-curated, verified, spam-frei."
    ),
    "language": "de",
    "locale": "Berlin, DE",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",  # falls back to NULL if missing
    "feeds": [
        # (url, title, site_url, favicon_url, note)
        ("https://www.rbb24.de/index.xml/feed=rss.xml",
         "rbb24 — Berlin & Brandenburg", "https://www.rbb24.de/",
         "https://www.rbb24.de/favicon.ico",
         "Öffentlich-rechtlich. Regionalnachrichten aus Berlin & Brandenburg."),
        ("https://www.tagesspiegel.de/contentexport/feed/berlin",
         "Tagesspiegel — Berlin", "https://www.tagesspiegel.de/berlin/",
         "https://www.tagesspiegel.de/favicon.ico",
         "Berlin-Ressort der überregionalen Tageszeitung."),
        ("https://www.berliner-zeitung.de/feed.xml",
         "Berliner Zeitung", "https://www.berliner-zeitung.de/",
         "https://www.berliner-zeitung.de/favicon.ico",
         "Traditionsreiche Berliner Tageszeitung."),
        ("https://www.morgenpost.de/berlin/rss",
         "Berliner Morgenpost — Berlin", "https://www.morgenpost.de/berlin/",
         "https://www.morgenpost.de/favicon.ico",
         "Berlin-Fokus, konservativ-bürgerlich."),
        ("https://www.berliner-kurier.de/feed.xml",
         "Berliner Kurier", "https://www.berliner-kurier.de/",
         "https://www.berliner-kurier.de/favicon.ico",
         "Boulevard, aber vor Ort."),
        ("https://taz.de/!s=berlin/rss.xml",
         "taz — Berlin", "https://taz.de/Berlin/",
         "https://taz.de/favicon.ico",
         "Berlin-Ressort der taz. Linksliberal, kritisch."),
        ("https://www.nd-aktuell.de/rss/neues-deutschland.xml",
         "neues deutschland", "https://www.nd-aktuell.de/",
         "https://www.nd-aktuell.de/favicon.ico",
         "Sozialistische Tageszeitung mit starkem Berlin-Fokus."),
        ("https://www.jungewelt.de/feeds/newsticker.rss",
         "Junge Welt", "https://www.jungewelt.de/",
         "https://www.jungewelt.de/favicon.ico",
         "Marxistisch-orientierte Tageszeitung aus Berlin."),
        ("https://berlinspectator.com/feed/",
         "The Berlin Spectator", "https://berlinspectator.com/",
         "https://berlinspectator.com/favicon.ico",
         "Independent English-language reporting on Berlin & Germany."),
        ("https://www.tip-berlin.de/feed/",
         "tip berlin", "https://www.tip-berlin.de/",
         "https://www.tip-berlin.de/favicon.ico",
         "Stadtmagazin: Kultur, Ausgehen, Politik."),
        ("https://www.notesofberlin.com/feed/",
         "Notes of Berlin", "https://www.notesofberlin.com/",
         "https://www.notesofberlin.com/favicon.ico",
         "Skurrile Beobachtungen aus dem Berliner Alltag."),
        ("https://www.loky.news/rss/",
         "loky (Prenzlauer Berg Nachrichten)", "https://www.loky.news/",
         "https://www.loky.news/favicon.ico",
         "Hyperlokaler Newsletter für Prenzlauer Berg."),
    ],
}


SEED_BERLIN_ENGLISH = {
    "slug": "berlin-english",
    "name": "Berlin — English",
    "description": (
        "English-language news + expat-focused feeds about Berlin and "
        "Germany. Curated for the many Berliners whose day-to-day "
        "reading language isn't German."
    ),
    "language": "en",
    "locale": "Berlin, DE",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://www.berlin.de/en/news/rubric.rss",
         "berlin.de — News (English)", "https://www.berlin.de/en/",
         "https://www.berlin.de/favicon.ico",
         "Official English-language press releases from the Berlin Senate."),
        ("https://berlinspectator.com/feed/",
         "The Berlin Spectator", "https://berlinspectator.com/",
         "https://berlinspectator.com/favicon.ico",
         "Independent English-language reporting on Berlin & Germany."),
        ("https://rss.dw.com/rdf/rss-en-ger",
         "Deutsche Welle — Germany (EN)", "https://www.dw.com/en/germany/",
         "https://www.dw.com/favicon.ico",
         "DW's Germany desk in English — national context expats read."),
        # Added 0.10.4 from prod signal — 5 users had already subscribed
        # to it manually, making it the second-most-subscribed Berlin
        # feed on the instance.
        ("https://allaboutberlin.com/guides/feed.rss",
         "All About Berlin — Guides", "https://allaboutberlin.com/",
         "https://allaboutberlin.com/favicon.ico",
         "The definitive English-language practical guides for living in "
         "Berlin — visas, taxes, apartments, health insurance."),
    ],
}


SEED_BETTER_SWE = {
    "slug": "better-software-engineer",
    "name": "Better Software Engineer",
    "description": (
        "Timeless English-language voices on shipping better software: "
        "systems, incidents, testing, tooling, and the craft. Hand-picked "
        "blogs and newsletters, no framework-flavour-of-the-week noise."
    ),
    "language": "en",
    "locale": "",  # global, no locale
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://simonwillison.net/atom/everything/",
         "Simon Willison's Weblog", "https://simonwillison.net/",
         "https://simonwillison.net/favicon.ico",
         "Notes on tools + AI + coding, from the co-creator of Django."),
        ("https://jvns.ca/atom.xml",
         "Julia Evans", "https://jvns.ca/",
         "https://jvns.ca/favicon.ico",
         "Systems knowledge in patient, illustrated bites. Wizard-zine energy."),
        ("https://danluu.com/atom.xml",
         "Dan Luu", "https://danluu.com/",
         "https://danluu.com/favicon.ico",
         "Deep, empirical writing on latency, reliability, and hiring."),
        ("https://overreacted.io/rss.xml",
         "overreacted (Dan Abramov)", "https://overreacted.io/",
         "https://overreacted.io/favicon.ico",
         "React internals + mental models — for anyone who ships UIs."),
        ("https://martinfowler.com/feed.atom",
         "Martin Fowler", "https://martinfowler.com/",
         "https://martinfowler.com/favicon.ico",
         "Refactoring, architecture, patterns. Classic."),
        ("https://devblogs.microsoft.com/oldnewthing/feed",
         "The Old New Thing (Raymond Chen)", "https://devblogs.microsoft.com/oldnewthing/",
         "https://devblogs.microsoft.com/favicon.ico",
         "Windows quirks, backward-compat war stories, and API archaeology."),
        ("https://www.hillelwayne.com/index.xml",
         "Hillel Wayne", "https://www.hillelwayne.com/",
         "https://www.hillelwayne.com/favicon.ico",
         "Formal methods, TLA+, and 'crossover projects' between disciplines."),
        ("https://mitchellh.com/feed.xml",
         "Mitchell Hashimoto", "https://mitchellh.com/",
         "https://mitchellh.com/favicon.ico",
         "Founder-turned-hacker essays: Zig, Ghostty, Terraform lessons."),
        ("https://increment.com/feed.xml",
         "Increment (Stripe)", "https://increment.com/",
         "https://increment.com/favicon.ico",
         "Themed engineering essays — testing, on-call, distributed systems."),
        ("https://lwn.net/headlines/newrss",
         "LWN.net", "https://lwn.net/",
         "https://lwn.net/favicon.ico",
         "Kernel + free-software journalism. Depth you won't get elsewhere."),
        ("https://lobste.rs/rss",
         "Lobsters", "https://lobste.rs/",
         "https://lobste.rs/favicon.ico",
         "Invite-only tech link aggregator. Signal-heavy compared to HN."),
        ("https://charity.wtf/feed/",
         "charity.wtf (Charity Majors)", "https://charity.wtf/",
         "https://charity.wtf/favicon.ico",
         "Observability, on-call, and management from Honeycomb's CTO."),
    ],
}


SEED_TEHRAN_FARSI = {
    "slug": "tehran-farsi",
    "name": "Tehran — فارسی",
    "description": (
        "منابع خبری فارسی‌زبان درباره‌ی ایران — از بی‌بی‌سی فارسی، "
        "دویچه وله، ایران اینترنشنال و زمانه. Farsi + English reporting "
        "on Iran, curated to fix the abysmal Persian-language RSS "
        "discovery in most readers."
    ),
    "language": "fa",
    "locale": "Tehran, IR",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://feeds.bbci.co.uk/persian/rss.xml",
         "BBC فارسی", "https://www.bbc.com/persian",
         "https://www.bbc.com/favicon.ico",
         "پوشش گسترده از رویدادهای ایران و منطقه."),
        ("https://rss.dw.com/xml/rss-per-all",
         "دویچه وله فارسی", "https://www.dw.com/fa-ir",
         "https://www.dw.com/favicon.ico",
         "گزارش‌های آلمانی-بین‌المللی به فارسی."),
        ("https://www.iranintl.com/feed",
         "Iran International (ایران اینترنشنال)",
         "https://www.iranintl.com/", "https://www.iranintl.com/favicon.ico",
         "شبکه‌ی خبری فارسی‌زبان مستقر در لندن."),
        ("https://en.radiozamaneh.com/feed/",
         "Zamaneh Media (English)", "https://en.radiozamaneh.com/",
         "https://en.radiozamaneh.com/favicon.ico",
         "Dutch-hosted Persian-focused reporting — English edition."),
    ],
}


SEED_BETTER_PM = {
    "slug": "better-product-manager",
    "name": "Better Product Manager",
    "description": (
        "English-language voices on product craft — strategy, prioritization, "
        "team-building, and the messy middle. Timeless essays and newsletters, "
        "not framework-of-the-week noise."
    ),
    "language": "en",
    "locale": "",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://www.lennysnewsletter.com/feed",
         "Lenny's Newsletter", "https://www.lennysnewsletter.com/",
         "https://www.lennysnewsletter.com/favicon.ico",
         "The de-facto PM newsletter. Interviews, frameworks, playbooks."),
        ("https://www.svpg.com/feed/",
         "Silicon Valley Product Group (Marty Cagan)",
         "https://www.svpg.com/", "https://www.svpg.com/favicon.ico",
         "Marty Cagan's essays — INSPIRED / EMPOWERED / TRANSFORMED author."),
        ("https://joulee.medium.com/feed",
         "Julie Zhuo", "https://medium.com/@joulee",
         "https://cdn-images-1.medium.com/favicon.ico",
         "Former Facebook design lead. Management + product craft."),
        ("https://cutlefish.substack.com/feed",
         "The Beautiful Mess (John Cutler)",
         "https://cutlefish.substack.com/", "https://substackcdn.com/favicon.ico",
         "Discovery + delivery + team dynamics, with hand-drawn diagrams."),
        ("https://www.bringthedonuts.com/feed.xml",
         "Bring the Donuts (Ken Norton)",
         "https://www.bringthedonuts.com/",
         "https://www.bringthedonuts.com/favicon.ico",
         "Former Google Ventures PM. Career + craft essays."),
        ("https://shreyasdoshi.substack.com/feed",
         "Shreyas Doshi", "https://shreyasdoshi.substack.com/",
         "https://substackcdn.com/favicon.ico",
         "Ex-Stripe/Twitter/Google PM. Sharp, opinionated frameworks."),
        ("https://ravimehta.substack.com/feed",
         "Ravi on Product", "https://ravimehta.substack.com/",
         "https://substackcdn.com/favicon.ico",
         "Career growth, org design, and product strategy."),
    ],
}


SEED_BERLIN_KULTUR = {
    "slug": "berlin-kultur",
    "name": "Berlin — Kultur",
    "description": (
        "Kunst, Musik, Theater, Ausstellungen und Ausgehen in Berlin. "
        "Öffentlich-rechtliches Kulturprogramm neben Stadtmagazinen und "
        "unabhängigen Kunstheften."
    ),
    "language": "de",
    "locale": "Berlin, DE",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://www.rbb24.de/kultur/index.xml/feed=rss.xml",
         "rbb24 — Kultur", "https://www.rbb24.de/kultur/",
         "https://www.rbb24.de/favicon.ico",
         "Öffentlich-rechtliches Kulturprogramm für Berlin & Brandenburg."),
        ("https://www.tip-berlin.de/feed/",
         "tip berlin", "https://www.tip-berlin.de/",
         "https://www.tip-berlin.de/favicon.ico",
         "Stadtmagazin: Kultur, Ausgehen, Konzerte, Theater."),
        ("https://www.zitty.de/feed/",
         "ZITTY", "https://www.zitty.de/",
         "https://www.zitty.de/favicon.ico",
         "Zweitältestes Berliner Stadtmagazin — Musik, Film, Nachtleben."),
        ("https://www.monopol-magazin.de/rss.xml",
         "Monopol — Kunstmagazin",
         "https://www.monopol-magazin.de/",
         "https://www.monopol-magazin.de/favicon.ico",
         "Zeitgenössische Kunst, Ausstellungen, Interviews mit Künstler:innen."),
        ("https://missy-magazine.de/feed/",
         "Missy Magazine", "https://missy-magazine.de/",
         "https://missy-magazine.de/favicon.ico",
         "Feministisches Pop-Kultur-Magazin aus Berlin."),
    ],
}


SEED_STARTUP_CRAFT = {
    "slug": "startup-craft",
    "name": "Startup Craft",
    "description": (
        "Timeless English-language voices on building companies — bootstrapped, "
        "VC-backed, and everything in between. Craft over hype, essays over "
        "tweetstorms, opinions worth marinating on."
    ),
    "language": "en",
    "locale": "",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("http://www.aaronsw.com/2002/feeds/pgessays.rss",
         "Paul Graham — Essays", "https://paulgraham.com/",
         "https://paulgraham.com/favicon.ico",
         "The canon. YC co-founder's essays via Aaron Swartz's mirror."),
        ("https://world.hey.com/dhh/feed.atom",
         "David Heinemeier Hansson (DHH)",
         "https://world.hey.com/dhh",
         "https://world.hey.com/favicon.ico",
         "37signals/Basecamp co-founder. Bootstrapped, opinionated, anti-hype."),
        ("https://world.hey.com/jason/feed.atom",
         "Jason Fried",
         "https://world.hey.com/jason",
         "https://world.hey.com/favicon.ico",
         "Same shop as DHH — product philosophy from the Basecamp side."),
        ("https://blog.samaltman.com/posts.atom",
         "Sam Altman", "https://blog.samaltman.com/",
         "https://blog.samaltman.com/favicon.ico",
         "YC → OpenAI. Startup essays that shaped a generation."),
        ("https://nathanbarry.com/feed/",
         "Nathan Barry (ConvertKit / Kit)",
         "https://nathanbarry.com/", "https://nathanbarry.com/favicon.ico",
         "Founder of Kit (née ConvertKit). Bootstrapped-to-scale journey."),
        ("https://levels.io/rss/",
         "Pieter Levels (@levelsio)",
         "https://levels.io/", "https://levels.io/favicon.ico",
         "Nomad List / Photo AI. Solo-builder posts + Twitter archives."),
    ],
}


SEED_LONGFORM = {
    "slug": "longform-reads",
    "name": "Longform Reads",
    "description": (
        "Deep essays and criticism for readers who want to marinate, "
        "not scroll. Ideas magazines, literary reviews, and the "
        "internet's most patient writers — no daily churn, no "
        "backlog shame."
    ),
    "language": "en",
    "locale": "",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://longreads.com/feed/",
         "Longreads", "https://longreads.com/",
         "https://longreads.com/favicon.ico",
         "The name says it. Curated longform journalism, no listicles."),
        ("https://aeon.co/feed.rss",
         "Aeon", "https://aeon.co/",
         "https://aeon.co/favicon.ico",
         "Ideas, philosophy, science — essays that pay attention."),
        ("https://www.lrb.co.uk/feeds/rss",
         "London Review of Books", "https://www.lrb.co.uk/",
         "https://www.lrb.co.uk/favicon.ico",
         "The UK's premier literary + political review. Long, opinionated, essential."),
        ("https://www.newyorker.com/feed/everything",
         "The New Yorker", "https://www.newyorker.com/",
         "https://www.newyorker.com/favicon.ico",
         "Everything the New Yorker publishes — reporting, essays, fiction, cartoons."),
        ("https://www.theatlantic.com/feed/channel/ideas/",
         "The Atlantic — Ideas",
         "https://www.theatlantic.com/ideas/",
         "https://www.theatlantic.com/favicon.ico",
         "The Atlantic's ideas + culture desk. American-flavored, well-argued."),
        ("https://www.nplusonemag.com/feed",
         "n+1", "https://www.nplusonemag.com/",
         "https://www.nplusonemag.com/favicon.ico",
         "Literary + cultural criticism from Brooklyn. Sharp, unafraid."),
        ("https://www.themarginalian.org/feed/",
         "The Marginalian (Maria Popova)",
         "https://www.themarginalian.org/",
         "https://www.themarginalian.org/favicon.ico",
         "Long essays on books, art, philosophy — one meditation at a time."),
        ("https://craigmod.com/index.xml",
         "Craig Mod", "https://craigmod.com/",
         "https://craigmod.com/favicon.ico",
         "Walking essays from Japan. Photography, books, wandering."),
        ("https://kottke.org/index.xml",
         "kottke.org", "https://kottke.org/",
         "https://kottke.org/favicon.ico",
         "Jason Kottke's blog — the internet's original curator, 25+ years running."),
        ("https://www.damninteresting.com/feed/",
         "Damn Interesting", "https://www.damninteresting.com/",
         "https://www.damninteresting.com/favicon.ico",
         "Meticulously researched narrative essays on the strange + true."),
    ],
}


SEED_ENGINEER_WEBLOGS = {
    "slug": "engineer-weblogs",
    "name": "Engineer Weblogs",
    "description": (
        "Thoughtful engineers writing about their craft — from household "
        "names like Julia Evans, Simon Willison and Dan Luu to lesser-known "
        "personal blogs whose posts stick with you. Seeded from the feeds real "
        "rssFriends users have already been reading."
    ),
    "language": "en",
    "locale": "",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://dave.cheney.net/feed",
         "Dave Cheney", "https://dave.cheney.net/",
         "https://dave.cheney.net/favicon.ico",
         "Go internals, performance, engineering practice."),
        ("https://drewdevault.com/blog/index.xml",
         "Drew DeVault", "https://drewdevault.com/",
         "https://drewdevault.com/favicon.ico",
         "Open-source maintainer (SourceHut, sway); frank essays on the craft + community."),
        ("https://scottchacon.com/atom.xml",
         "Scott Chacon", "https://scottchacon.com/",
         "https://scottchacon.com/favicon.ico",
         "Git co-founder, ex-GitHub. Notes from someone who has shipped a lot."),
        ("https://endler.dev/rss.xml",
         "Matthias Endler", "https://endler.dev/",
         "https://endler.dev/favicon.ico",
         "Rust + Go + Corrode. Practical engineering essays."),
        ("https://jesseduffield.com/feed.xml",
         "Jesse Duffield (Pursuit Of Laziness)",
         "https://jesseduffield.com/",
         "https://jesseduffield.com/favicon.ico",
         "Creator of lazygit + lazydocker. Terminal-UI thinking + Go internals."),
        ("https://toot.cafe/@codepo8.rss",
         "Chris Heilmann", "https://toot.cafe/@codepo8",
         "https://toot.cafe/favicon.ico",
         "Ex-Mozilla dev advocate; the web platform + accessibility "
         "from someone who's shipped it."),
        ("https://www.arp242.net/feed.xml",
         "arp242.net (Martin Tournoij)",
         "https://www.arp242.net/",
         "https://www.arp242.net/favicon.ico",
         "Go, Unix, quirky opinions. Author of goatcounter."),
        ("https://ellie.wtf/index.xml",
         "Ellie's Notes", "https://ellie.wtf/",
         "https://ellie.wtf/favicon.ico",
         "Compilers, formal methods, and cursed programming languages, illustrated."),
        ("http://jackkelly.name/blog/rss.xml",
         "Jack Kelly", "http://jackkelly.name/",
         "http://jackkelly.name/favicon.ico",
         "Haskell, static analysis, homelab notes."),
        ("https://www.nicchan.me/feed.xml",
         "Nic Chan", "https://www.nicchan.me/",
         "https://www.nicchan.me/favicon.ico",
         "Front-end + design + accessibility."),
        ("https://noelberry.ca/rss.xml",
         "Noel Berry", "https://noelberry.ca/",
         "https://noelberry.ca/favicon.ico",
         "Indie game dev (Celeste), engine work, design notes."),
        ("https://brndn.io/index.xml",
         "Brenden's Blog", "https://brndn.io/",
         "https://brndn.io/favicon.ico",
         "Prolific personal blog on tools, side projects, and shipping."),
        ("https://blainsmith.com/rss.xml",
         "Blain Smith", "https://blainsmith.com/",
         "https://blainsmith.com/favicon.ico",
         "Backend engineering, tooling, and everyday craft."),
        ("https://rushter.com/blog/feed/",
         "Artem Golubin (rushter.com)",
         "https://rushter.com/",
         "https://rushter.com/favicon.ico",
         "Python internals + performance deep-dives."),
        ("https://notes.pault.ag/index.xml",
         "Paul's Notes (pault.ag)",
         "https://notes.pault.ag/",
         "https://notes.pault.ag/favicon.ico",
         "Systems, security, packaging. Debian dev + reverse engineering."),
        ("https://kristina-pianykh.github.io/blog/index.xml",
         "Kristina Pianykh (Tada!)",
         "https://kristina-pianykh.github.io/blog/",
         "https://kristina-pianykh.github.io/favicon.ico",
         "Learning-in-public log from a working engineer."),
        ("https://iamyekta.com/post/index.xml",
         "Yekta Leblebici", "https://iamyekta.com/",
         "https://iamyekta.com/favicon.ico",
         "Distributed systems + everyday backend work notes."),
        ("https://www.loopwerk.io/articles/feed.xml",
         "Loopwerk (Kevin Renskers)",
         "https://www.loopwerk.io/",
         "https://www.loopwerk.io/favicon.ico",
         "Swift, Python, iOS. Prolific with ~150 posts on record."),
        ("https://zachholman.com/atom.xml",
         "Zach Holman", "https://zachholman.com/",
         "https://zachholman.com/favicon.ico",
         "Early GitHub employee. Startup + engineering culture essays."),
        ("https://lea.verou.me/feed.xml",
         "Lea Verou", "https://lea.verou.me/",
         "https://lea.verou.me/favicon.ico",
         "CSS wizard + web-standards contributor. Long-form technical writing."),
        ("https://kentcdodds.com/blog/rss.xml",
         "Kent C. Dodds", "https://kentcdodds.com/",
         "https://kentcdodds.com/favicon.ico",
         "React, testing, teaching. Prolific + practical."),
        # 0.11.9: top-gun round — nine household-name engineer blogs,
        # each verified active as of 2026-08-04. See docs/collections.
        ("https://jvns.ca/atom.xml",
         "Julia Evans", "https://jvns.ca/",
         "https://jvns.ca/favicon.ico",
         "Debugging, systems, comics + zines. Universally beloved teacher."),
        ("https://simonwillison.net/atom/everything/",
         "Simon Willison", "https://simonwillison.net/",
         "https://simonwillison.net/favicon.ico",
         "Django co-creator. Now a leading voice on LLMs + Datasette."),
        ("https://danluu.com/atom.xml",
         "Dan Luu", "https://danluu.com/",
         "https://danluu.com/favicon.ico",
         "Long essays on engineering rigor, hiring, hardware bugs. HN-favorite."),
        ("https://martinfowler.com/feed.atom",
         "Martin Fowler", "https://martinfowler.com/",
         "https://martinfowler.com/favicon.ico",
         "Architecture, refactoring, DDD — from a foundational voice in the field."),
        ("https://rachelbythebay.com/w/atom.xml",
         "Rachel by the Bay", "https://rachelbythebay.com/",
         "https://rachelbythebay.com/favicon.ico",
         "SRE war stories from an ex-Facebook ops veteran. Cult reading."),
        ("https://fabiensanglard.net/rss.xml",
         "Fabien Sanglard", "https://fabiensanglard.net/",
         "https://fabiensanglard.net/favicon.ico",
         "Author of the Doom Black Book. Reverse-engineers legendary game engines."),
        ("https://www.schneier.com/feed/atom/",
         "Bruce Schneier", "https://www.schneier.com/",
         "https://www.schneier.com/favicon.ico",
         "The security industry's most public voice for 20+ years."),
        ("https://charity.wtf/feed/",
         "Charity Majors", "https://charity.wtf/",
         "https://charity.wtf/favicon.ico",
         "Observability, Honeycomb founder, punchy engineering-management essays."),
        ("https://brooker.co.za/blog/atom.xml",
         "Marc Brooker", "https://brooker.co.za/",
         "https://brooker.co.za/favicon.ico",
         "AWS distributed-systems fellow. Cited when you argue about consistency."),
    ],
}


SEED_TOOL_CREATORS = {
    "slug": "tool-creators",
    "name": "Tool Creators",
    "description": (
        "Reading the people who built the tools you use every day. Personal "
        "blogs from the creators of Ruby on Rails, Terraform, Redis, ripgrep, "
        "Extreme Programming / TDD, Git, and parts of Go. Not corporate blogs "
        "— firsthand accounts from the humans behind the software."
    ),
    "language": "en",
    "locale": "",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://world.hey.com/dhh/feed.atom",
         "DHH (David Heinemeier Hansson)",
         "https://world.hey.com/dhh",
         "https://world.hey.com/favicon.ico",
         "Creator of Ruby on Rails. Co-founder of 37signals + HEY."),
        ("https://mitchellh.com/feed.xml",
         "Mitchell Hashimoto", "https://mitchellh.com/",
         "https://mitchellh.com/favicon.ico",
         "Creator of Terraform, Vagrant, Vault, Nomad. Co-founder of HashiCorp."),
        ("http://antirez.com/rss",
         "antirez (Salvatore Sanfilippo)", "http://antirez.com/",
         "http://antirez.com/favicon.ico",
         "Creator of Redis. Systems programming and engineering-life essays."),
        ("https://tidyfirst.substack.com/feed",
         "Kent Beck (Tidy First?)", "https://tidyfirst.substack.com/",
         "https://substackcdn.com/icons/substack/favicon.ico",
         "Creator of JUnit, TDD, and Extreme Programming. Software design essays."),
        ("https://blog.burntsushi.net/index.xml",
         "Andrew Gallant (burntsushi)",
         "https://blog.burntsushi.net/",
         "https://blog.burntsushi.net/favicon.ico",
         "Creator of ripgrep and Rust's regex crate. Deep Rust engineering essays."),
        ("https://research.swtch.com/feed.atom",
         "Russ Cox (research!rsc)",
         "https://research.swtch.com/",
         "https://research.swtch.com/favicon.ico",
         "Go tech lead; author of RE2 and govulncheck. Landmark essays on language design."),
        # Dual-listed with engineer-weblogs — Scott is the git co-founder
        # so he anchors the "creator of" theme too.
        ("https://scottchacon.com/atom.xml",
         "Scott Chacon", "https://scottchacon.com/",
         "https://scottchacon.com/favicon.ico",
         "Git co-founder, ex-GitHub. Notes from someone who has shipped a lot."),
    ],
}


SEED_CURIOUS_MINDS = {
    "slug": "curious-minds",
    "name": "Curious Minds",
    "description": (
        "Ideas, wonder, and the pleasure of thinking. Science, philosophy, "
        "psychology, and internet essays that expand how you see the world. "
        "Video, text, and everything in between — for people who read to "
        "grow, not to keep up."
    ),
    "language": "en",
    "locale": "",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCsXVk37bltHxD1rDPwtNM8Q",
         "Kurzgesagt – In a Nutshell", "https://kurzgesagt.org/",
         "https://www.youtube.com/favicon.ico",
         "Animated deep-dives into science, philosophy, and existential questions."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCvQECJukTDE2i6aCoMnS-Vg",
         "Big Think", "https://bigthink.com/",
         "https://www.youtube.com/favicon.ico",
         "Short-form interviews with scientists, thinkers, and cultural figures."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UC3JhfsgFPLSLNEROQCdj-GQ",
         "Headspace", "https://www.headspace.com/",
         "https://www.youtube.com/favicon.ico",
         "Mindfulness, sleep, focus. Short clips distilling a decade of guided meditation."),
        ("https://waitbutwhy.com/feed",
         "Wait But Why (Tim Urban)", "https://waitbutwhy.com/",
         "https://waitbutwhy.com/favicon.ico",
         "Long, stick-figured essays on AI, procrastination, Elon, and the human condition."),
        ("https://www.quantamagazine.org/feed/",
         "Quanta Magazine", "https://www.quantamagazine.org/",
         "https://www.quantamagazine.org/favicon.ico",
         "Rigorous science journalism — math, physics, biology, computer science."),
        ("https://nautil.us/feed/",
         "Nautilus", "https://nautil.us/",
         "https://nautil.us/favicon.ico",
         "Long-form science + culture essays. Slow reading, worth it."),
        ("https://fs.blog/feed/",
         "Farnam Street", "https://fs.blog/",
         "https://fs.blog/favicon.ico",
         "Mental models, decision-making, learning how to think."),
        ("https://astralcodexten.substack.com/feed",
         "Astral Codex Ten (Scott Alexander)",
         "https://astralcodexten.substack.com/",
         "https://substackcdn.com/favicon.ico",
         "Psychiatry, rationality, book reviews, and predictions."),
        ("https://www.themarginalian.org/feed/",
         "The Marginalian (Maria Popova)",
         "https://www.themarginalian.org/",
         "https://www.themarginalian.org/favicon.ico",
         "Meditations on art, science, and how to live — daily since 2006."),
    ],
}


SEED_TECH_YOUTUBE = {
    "slug": "tech-on-youtube",
    "name": "Tech on YouTube",
    "description": (
        "Great tech YouTube channels, subscribable via RSS. "
        "Homelabs, systems talks, tutorials, and long-form documentaries — "
        "the good stuff, without the recommendation algorithm."
    ),
    "language": "en",
    "locale": "",
    "is_official": True,
    "curator_handle": "ahmadian-farid-1988",
    "feeds": [
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCR-DXc1voovS8nhAvccRZhg",
         "Jeff Geerling", "https://www.jeffgeerling.com/",
         "https://www.youtube.com/favicon.ico",
         "Homelab, Ansible, Raspberry Pi. Practical + patient."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCCezIgC97PvUuR4_gbFUs5g",
         "Corey Schafer", "https://coreyms.com/",
         "https://www.youtube.com/favicon.ico",
         "Deep Python tutorials from the fundamentals up."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCKWaEZ-_VweaEx1j62do_vQ",
         "IBM Technology", "https://www.ibm.com/",
         "https://www.youtube.com/favicon.ico",
         "Whiteboard explainers for cloud, AI, and enterprise systems."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCGKEMK3s-ZPbjVOIuAV8clQ",
         "Core Dumped", "https://www.youtube.com/@CoreDumpped",
         "https://www.youtube.com/favicon.ico",
         "OS internals, compilers, systems — animated deep-dives."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UC0fDG3byEcMtbOqPMymDNbw",
         "/noclip", "https://noclip.video/",
         "https://www.youtube.com/favicon.ico",
         "Documentaries about how games (and their makers) come to life."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCsUalyRg43M8D60mtHe6YcA",
         "CultRepo (Coding Tech)",
         "https://www.youtube.com/@CultRepo",
         "https://www.youtube.com/favicon.ico",
         "Conference talks + long-form tech education."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UChWB95_-n9rUc3H9srsn9bQ",
         "Bplus Podcast", "https://www.youtube.com/@BplusPodcast",
         "https://www.youtube.com/favicon.ico",
         "Interviews with engineers, founders, and other builders."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA",
         "Fireship", "https://fireship.io/",
         "https://www.youtube.com/favicon.ico",
         "100-second tech explainers + weekly news."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UC8ENHE5xdFSwx71u3fDH5Xw",
         "ThePrimeagen", "https://www.youtube.com/@ThePrimeagen",
         "https://www.youtube.com/favicon.ico",
         "Vim + Rust + rants. Loud, opinionated, sometimes right."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCS0N5baNlQWJCUrhCEo8WlA",
         "Ben Eater", "https://eater.net/",
         "https://www.youtube.com/favicon.ico",
         "Building an 8-bit CPU on a breadboard. The gold standard for hands-on hardware."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UC9x0AN7BWHpCDHSm9NiJFJQ",
         "NetworkChuck", "https://networkchuck.com/",
         "https://www.youtube.com/favicon.ico",
         "Networking, cybersecurity, and cloud, with high energy."),
        ("https://www.youtube.com/feeds/videos.xml?channel_id=UCP7WmQ_U4GB3K51Od9QvM0w",
         "David Bombal", "https://davidbombal.com/",
         "https://www.youtube.com/favicon.ico",
         "Networking + hacking + career, with heavyweight guest interviews."),
    ],
}


ALL_SEED_COLLECTIONS = (
    SEED_BERLIN_NACHRICHTEN,
    SEED_BERLIN_ENGLISH,
    SEED_BERLIN_KULTUR,
    SEED_BETTER_SWE,
    SEED_BETTER_PM,
    SEED_STARTUP_CRAFT,
    SEED_TEHRAN_FARSI,
    SEED_LONGFORM,
    SEED_ENGINEER_WEBLOGS,
    SEED_TOOL_CREATORS,
    SEED_CURIOUS_MINDS,
    SEED_TECH_YOUTUBE,
)


def _seed_official_collections() -> None:
    """Idempotently create + sync the seed Collection(s). Runs on every
    startup with these guarantees:

      - Collection missing → created with all its declared feeds.
      - Collection present, seed adds a NEW url → CollectionFeed added.
      - Collection present, seed lists a URL that was previously removed
        by the curator via the UI → still re-added (we don't distinguish
        "never had" from "removed"; this trade-off is documented as a
        known bug on the same shape as CategoryFollow, see 0.9.5).
      - Feed rows are deduped globally by URL (existing UNIQUE constraint).

    Gated by env var `RSSFRIENDS_SEED_COLLECTIONS` (default '1'):
    tests set this to '0' so the seeded Feed rows don't pollute test
    setups that count/iterate Feed rows. Tests that want the seed
    (tests/test_collections.py) call this function explicitly."""
    import os
    from datetime import UTC, datetime

    if os.environ.get("RSSFRIENDS_SEED_COLLECTIONS", "1") == "0":
        return

    with Session(engine) as db:
        for seed in ALL_SEED_COLLECTIONS:
            col = db.exec(
                select(Collection).where(Collection.slug == seed["slug"])
            ).first()

            if col is None:
                # New collection — create it.
                curator_id: int | None = None
                curator_user = db.exec(
                    select(User).where(User.handle == seed["curator_handle"])
                ).first()
                if curator_user:
                    curator_id = curator_user.id
                col = Collection(
                    slug=seed["slug"],
                    name=seed["name"],
                    description=seed["description"],
                    language=seed["language"],
                    locale=seed["locale"],
                    curator_id=curator_id,
                    is_official=seed["is_official"],
                    is_public=True,
                    created_at=datetime.now(UTC),
                )
                db.add(col)
                db.flush()  # get col.id

            # Existing CollectionFeed URLs on this collection → skip
            # re-adding them (idempotent).
            existing_urls = set(db.exec(
                select(Feed.url)
                .join(CollectionFeed, CollectionFeed.feed_id == Feed.id)
                .where(CollectionFeed.collection_id == col.id)
            ).all())
            # 0.11.4: also honor tombstones — a URL a curator explicitly
            # removed via the UI must stay removed across deploys, even
            # though it's still in the seed dict below. This is the
            # fix for the resurrection bug reported by
            # @ahmadian-farid-1988 on 2026-08-03 for tehran-farsi.
            tombstoned_urls = set(db.exec(
                select(CollectionFeedTombstone.feed_url)
                .where(CollectionFeedTombstone.collection_id == col.id)
            ).all())

            # Sort_order continues from where the previous batch left off,
            # so newly-appended seed feeds land at the bottom.
            max_order = db.exec(
                select(CollectionFeed.sort_order)
                .where(CollectionFeed.collection_id == col.id)
                .order_by(CollectionFeed.sort_order.desc())
            ).first() or 0

            for i, (url, title, site_url, favicon_url, note) in enumerate(seed["feeds"]):
                if url in existing_urls:
                    continue
                if url in tombstoned_urls:
                    continue
                feed = db.exec(select(Feed).where(Feed.url == url)).first()
                if not feed:
                    feed = Feed(
                        url=url, title=title, site_url=site_url,
                        favicon_url=favicon_url,
                    )
                    db.add(feed)
                    db.flush()
                db.add(CollectionFeed(
                    collection_id=col.id,
                    feed_id=feed.id,
                    note=note,
                    sort_order=max_order + 1 + i,
                ))
        db.commit()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
