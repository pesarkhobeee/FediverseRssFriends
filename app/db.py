from collections.abc import Iterator
from datetime import UTC, datetime

from sqlmodel import Field, Session, SQLModel, create_engine

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


def _now() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    handle: str = Field(index=True, unique=True)
    visibility: str = Field(default="public")  # public | unlisted | private
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


def init_db() -> None:
    # SQLite: WAL for better concurrency under a single process.
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
