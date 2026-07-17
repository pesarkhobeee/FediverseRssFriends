from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine

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


class Favorite(SQLModel, table=True):
    """A user starring a specific FeedItem. Composite PK deduplicates."""

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


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
