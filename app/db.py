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
    # 0.11.17: ActivityPub actor keypair. Empty until the user's Actor
    # JSON is first requested (or explicitly minted). PEM-encoded.
    # NEVER exposed publicly for the private key — the public key is
    # shipped inside the Actor document as `publicKey.publicKeyPem`.
    ap_private_key: str = Field(default="",
                                sa_column_kwargs={"server_default": ""})
    ap_public_key: str = Field(default="",
                               sa_column_kwargs={"server_default": ""})


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


class RemoteActor(SQLModel, table=True):
    """0.11.18: cache of remote ActivityPub actors we've seen inbound
    from (via Follow) or dispatched outbound to. We stash the public
    key + inbox URL so signature verification and delivery don't need
    a fresh HTTPS round-trip on every message."""

    __table_args__ = (
        UniqueConstraint("actor_url", name="uq_remoteactor_url"),
    )

    id: int | None = Field(default=None, primary_key=True)
    actor_url: str = Field(index=True)      # e.g. https://mastodon.social/users/foo
    inbox_url: str                          # POST target for delivery
    public_key_pem: str                     # for verifying their signatures
    key_id: str = Field(default="")         # e.g. actor_url#main-key
    preferred_username: str = Field(default="")
    display_name: str = Field(default="")
    fetched_at: datetime = Field(default_factory=_now)


class RemoteFollowing(SQLModel, table=True):
    """0.11.30 (M10): a local rssFriends user follows a remote AP
    actor (e.g. @simon@simonwillison.net). Complements RemoteFollow
    which is the inbound direction (a remote actor follows us).
    accepted_at NULL = we sent Follow, haven't seen Accept yet."""

    __table_args__ = (
        UniqueConstraint("user_id", "remote_actor_id",
                         name="uq_remotefollowing_pair"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    remote_actor_id: int = Field(foreign_key="remoteactor.id", index=True)
    follow_activity_id: str        # ID of the Follow we sent
    accepted_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class RemoteItem(SQLModel, table=True):
    """0.11.30 (M10): a Note delivered to our inbox from a remote
    actor we follow (or that we've cached via someone else's
    interaction). Rendered on the friends timeline alongside local
    Activity rows."""

    __table_args__ = (
        UniqueConstraint("ap_note_id", name="uq_remoteitem_ap_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    remote_actor_id: int = Field(foreign_key="remoteactor.id", index=True)
    ap_note_id: str                  # object.id — dedup key
    content_html: str = Field(default="")
    note_url: str = Field(default="")     # Note.url — link to original
    published_at: datetime
    received_at: datetime = Field(default_factory=_now)


class RemoteFollow(SQLModel, table=True):
    """0.11.18: a remote AP actor has followed one of our users. We
    accept and store; new activities from that user are delivered to
    the remote actor's inbox."""

    __table_args__ = (
        UniqueConstraint("remote_actor_id", "user_id",
                         name="uq_remotefollow_pair"),
    )

    id: int | None = Field(default=None, primary_key=True)
    remote_actor_id: int = Field(foreign_key="remoteactor.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    follow_activity_id: str                 # id of the Follow activity we accepted
    accepted_at: datetime = Field(default_factory=_now)
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

    # 0.11.17: ActivityPub actor keypair columns on User. Both nullable
    # with '' default — a key is generated lazily on the first Actor
    # request via _ensure_user_keypair(). Keeps schema back-compat +
    # doesn't force keys on users who never receive an AP request.
    if _sqlite_column_exists(conn, "user", "id"):
        for col in ("ap_private_key", "ap_public_key"):
            if not _sqlite_column_exists(conn, "user", col):
                conn.exec_driver_sql(
                    f"ALTER TABLE user ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )

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



# ─── Seed community-curated collections ──────────────────────────────
#
# Add dictionaries here to auto-provision Collection rows on every
# startup. Each dict follows the shape below; _seed_official_collections
# is idempotent, so listing a seed is safe across restarts. This fork
# ships empty on purpose — self-hosters populate their own.
#
#   EXAMPLE_SEED = {
#       "slug": "example-tech",
#       "name": "Example — Tech",
#       "description": "…one-sentence blurb…",
#       "language": "en",
#       "locale": "Global",
#       "is_official": True,
#       "curator_handle": None,   # or a handle string; falls back to NULL if missing
#       "feeds": [ (url, title, site_url, favicon_url, note), … ],
#   }

ALL_SEED_COLLECTIONS: tuple[dict, ...] = ()




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
            # Honor tombstones: a URL a curator explicitly removed via
            # the UI must stay removed across deploys, even though it's
            # still in the seed dict below.
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
