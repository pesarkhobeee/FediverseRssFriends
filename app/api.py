"""Read-only public JSON API, mounted at /api/v1 (docs/public-api.md).

Anonymous by design: nothing here reads the session, so every caller
gets the same response and a CDN may cache it. Every handle goes
through `can_view(None, …)`; private users are 404, same as unknown.
Responses are built from explicit models, never from table rows, so a
new column on `User` or `Subscription` can't leak by accident.
"""

import base64
import binascii
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import true
from sqlmodel import Session, and_, func, or_, select

from . import __version__, config
from .db import (
    Activity,
    Bookmark,
    Category,
    Collection,
    CollectionFeed,
    Feed,
    FeedItem,
    Subscription,
    User,
    get_session,
)
from .public import can_view, resolve_handle

API_VERSION = "1"
PAGE_DEFAULT = 50
PAGE_MAX = 100

api = FastAPI(
    title=f"{config.INSTANCE_NAME} public API",
    version=API_VERSION,
    description="Read-only data that is already public on the HTML pages.",
)
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_credentials=False,
)


class Instance(BaseModel):
    name: str
    description: str
    version: str
    api_version: str
    users: int
    feeds: int


class UserProfile(BaseModel):
    handle: str
    url: str
    visibility: str
    created_at: datetime
    categories: int
    feeds: int


class CategoryOut(BaseModel):
    name: str
    feeds: int


class CategoryList(BaseModel):
    items: list[CategoryOut]
    next: str | None = None


class FeedOut(BaseModel):
    id: int
    title: str
    url: str
    site_url: str
    favicon_url: str
    category: str
    page_url: str


class FeedList(BaseModel):
    items: list[FeedOut]
    next: str | None = None


class ItemOut(BaseModel):
    id: int
    title: str
    link: str
    summary: str
    published_at: datetime
    feed_id: int
    feed_title: str
    feed_url: str


class RadarItemOut(ItemOut):
    category: str


class RadarItemList(BaseModel):
    items: list[RadarItemOut]
    next: str | None = None


class BookmarkOut(ItemOut):
    bookmarked_at: datetime


class BookmarkList(BaseModel):
    items: list[BookmarkOut]
    next: str | None = None


class ActivityOut(BaseModel):
    id: int
    kind: str
    subject: str
    subject_url: str
    created_at: datetime


class ActivityList(BaseModel):
    items: list[ActivityOut]
    next: str | None = None


class CollectionOut(BaseModel):
    slug: str
    name: str
    description: str
    language: str
    locale: str
    official: bool
    feed_count: int
    url: str


class CollectionList(BaseModel):
    items: list[CollectionOut]
    next: str | None = None


class CollectionFeedOut(BaseModel):
    id: int
    title: str
    url: str
    site_url: str
    favicon_url: str
    note: str


class CollectionDetail(CollectionOut):
    feeds: list[CollectionFeedOut]


Limit = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX)


def _encode_cursor(at: datetime, row_id: int) -> str:
    raw = f"{at.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, row_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(at), int(row_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(400, "Invalid cursor") from None


def _after(cursor: str | None, at_col, id_col):
    """Keyset condition for newest-first paging: rows strictly older than
    the cursor, so new items arriving between requests never shift pages."""
    if cursor is None:
        return true()
    at, row_id = _decode_cursor(cursor)
    return or_(at_col < at, and_(at_col == at, id_col < row_id))


def _item_fields(item: FeedItem, feed: Feed) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "link": item.link,
        "summary": item.summary,
        "published_at": item.published_at or item.fetched_at,
        "feed_id": feed.id,
        "feed_title": feed.title,
        "feed_url": feed.url,
    }


def _category_or_404(db: Session, owner: User, name: str) -> None:
    if not db.exec(
        select(Category.id).where(Category.user_id == owner.id, Category.name == name)
    ).first():
        raise HTTPException(404, "Category not found")


class _Redirect(Exception):
    def __init__(self, location: str):
        self.location = location


@api.exception_handler(_Redirect)
def _redirect(_: Request, exc: _Redirect):
    return RedirectResponse(exc.location, status_code=301)


def _visible_user(request: Request, handle: str, db: Session) -> User:
    owner = resolve_handle(db, handle)
    if owner is None or not can_view(None, owner):
        raise HTTPException(404, "Not found")
    if owner.handle != handle:
        new_path = request.url.path.replace(f"/users/{handle}", f"/users/{owner.handle}", 1)
        if request.url.query:
            new_path = f"{new_path}?{request.url.query}"
        raise _Redirect(new_path)
    return owner


def _count(db: Session, stmt) -> int:
    return db.exec(stmt).one()


@api.get("/instance", response_model=Instance)
def instance(db: Session = Depends(get_session)):
    return Instance(
        name=config.INSTANCE_NAME,
        description=config.INSTANCE_DESCRIPTION,
        version=__version__,
        api_version=API_VERSION,
        users=_count(db, select(func.count()).select_from(User)),
        feeds=_count(db, select(func.count()).select_from(Feed)),
    )


@api.get("/users/{handle}", response_model=UserProfile)
def user_profile(request: Request, handle: str, db: Session = Depends(get_session)):
    owner = _visible_user(request, handle, db)
    return UserProfile(
        handle=owner.handle,
        url=f"{config.BASE_URL}/u/{owner.handle}",
        visibility=owner.visibility,
        created_at=owner.created_at,
        categories=_count(
            db, select(func.count()).select_from(Category).where(Category.user_id == owner.id)
        ),
        feeds=_count(
            db,
            select(func.count()).select_from(Subscription).where(Subscription.user_id == owner.id),
        ),
    )


@api.get("/users/{handle}/categories", response_model=CategoryList)
def user_categories(request: Request, handle: str, db: Session = Depends(get_session)):
    owner = _visible_user(request, handle, db)
    rows = db.exec(
        select(Category.name, func.count(Subscription.id))
        .outerjoin(Subscription, Subscription.category_id == Category.id)
        .where(Category.user_id == owner.id)
        .group_by(Category.id)
        .order_by(Category.sort_order, Category.id)
    ).all()
    return CategoryList(items=[CategoryOut(name=name, feeds=n) for name, n in rows])


@api.get("/users/{handle}/feeds", response_model=FeedList)
def user_feeds(
    request: Request,
    handle: str,
    category: str | None = None,
    db: Session = Depends(get_session),
):
    owner = _visible_user(request, handle, db)
    stmt = (
        select(Feed, Category.name)
        .join(Subscription, Subscription.feed_id == Feed.id)
        .join(Category, Category.id == Subscription.category_id)
        .where(Subscription.user_id == owner.id)
    )
    if category is not None:
        _category_or_404(db, owner, category)
        stmt = stmt.where(Category.name == category)
    rows = db.exec(
        stmt.order_by(Category.sort_order, Category.id, Subscription.sort_order, Subscription.id)
    ).all()
    return FeedList(items=[
        FeedOut(
            id=feed.id,
            title=feed.title,
            url=feed.url,
            site_url=feed.site_url,
            favicon_url=feed.favicon_url,
            category=cat_name,
            page_url=f"{config.BASE_URL}/f/{feed.id}",
        )
        for feed, cat_name in rows
    ])


@api.get("/users/{handle}/items", response_model=RadarItemList)
def user_items(
    request: Request,
    handle: str,
    category: str | None = None,
    cursor: str | None = None,
    limit: int = Limit,
    db: Session = Depends(get_session),
):
    owner = _visible_user(request, handle, db)
    subs = select(Subscription.feed_id, Category.name).join(
        Category, Category.id == Subscription.category_id
    ).where(Subscription.user_id == owner.id)
    if category is not None:
        _category_or_404(db, owner, category)
        subs = subs.where(Category.name == category)
    category_by_feed = dict(db.exec(subs).all())

    sort_at = func.coalesce(FeedItem.published_at, FeedItem.fetched_at)
    rows = db.exec(
        select(FeedItem, Feed)
        .join(Feed, Feed.id == FeedItem.feed_id)
        .where(FeedItem.feed_id.in_(category_by_feed.keys()))
        .where(_after(cursor, sort_at, FeedItem.id))
        .order_by(sort_at.desc(), FeedItem.id.desc())
        .limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = _encode_cursor(last.published_at or last.fetched_at, last.id)
    return RadarItemList(
        items=[
            RadarItemOut(**_item_fields(item, feed), category=category_by_feed[feed.id])
            for item, feed in page
        ],
        next=next_cursor,
    )


@api.get("/users/{handle}/bookmarks", response_model=BookmarkList)
def user_bookmarks(
    request: Request,
    handle: str,
    cursor: str | None = None,
    limit: int = Limit,
    db: Session = Depends(get_session),
):
    owner = _visible_user(request, handle, db)
    rows = db.exec(
        select(Bookmark, FeedItem, Feed)
        .join(FeedItem, FeedItem.id == Bookmark.feed_item_id)
        .join(Feed, Feed.id == FeedItem.feed_id)
        .where(Bookmark.user_id == owner.id)
        .where(_after(cursor, Bookmark.created_at, Bookmark.feed_item_id))
        .order_by(Bookmark.created_at.desc(), Bookmark.feed_item_id.desc())
        .limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = _encode_cursor(last.created_at, last.feed_item_id)
    return BookmarkList(
        items=[
            BookmarkOut(**_item_fields(item, feed), bookmarked_at=bm.created_at)
            for bm, item, feed in page
        ],
        next=next_cursor,
    )


@api.get("/users/{handle}/activity", response_model=ActivityList)
def user_activity(
    request: Request,
    handle: str,
    cursor: str | None = None,
    limit: int = Limit,
    db: Session = Depends(get_session),
):
    owner = _visible_user(request, handle, db)
    private_ids = select(User.id).where(User.visibility == "private")
    rows = db.exec(
        select(Activity)
        .where(Activity.user_id == owner.id)
        .where(~and_(Activity.ref_type == "user", Activity.ref_id.in_(private_ids)))
        .where(_after(cursor, Activity.created_at, Activity.id))
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = _encode_cursor(page[-1].created_at, page[-1].id)
    return ActivityList(
        items=[
            ActivityOut(
                id=a.id, kind=a.kind, subject=a.subject,
                subject_url=a.subject_url, created_at=a.created_at,
            )
            for a in page
        ],
        next=next_cursor,
    )


def _collection_out(col: Collection, feed_count: int) -> dict:
    return {
        "slug": col.slug,
        "name": col.name,
        "description": col.description,
        "language": col.language,
        "locale": col.locale,
        "official": col.is_official,
        "feed_count": feed_count,
        "url": f"{config.BASE_URL}/collections/{col.slug}",
    }


@api.get("/collections", response_model=CollectionList)
def collections(db: Session = Depends(get_session)):
    rows = db.exec(
        select(Collection, func.count(CollectionFeed.id))
        .outerjoin(CollectionFeed, CollectionFeed.collection_id == Collection.id)
        .where(Collection.is_public == True)  # noqa: E712
        .group_by(Collection.id)
        .order_by(Collection.is_official.desc(), Collection.created_at.desc())
    ).all()
    return CollectionList(items=[CollectionOut(**_collection_out(c, n)) for c, n in rows])


@api.get("/collections/{slug}", response_model=CollectionDetail)
def collection_detail(slug: str, db: Session = Depends(get_session)):
    col = db.exec(select(Collection).where(Collection.slug == slug)).first()
    if col is None or not col.is_public:
        raise HTTPException(404, "Not found")
    rows = db.exec(
        select(CollectionFeed, Feed)
        .join(Feed, Feed.id == CollectionFeed.feed_id)
        .where(CollectionFeed.collection_id == col.id)
        .order_by(CollectionFeed.sort_order, CollectionFeed.id)
    ).all()
    return CollectionDetail(
        **_collection_out(col, len(rows)),
        feeds=[
            CollectionFeedOut(
                id=feed.id, title=feed.title, url=feed.url, site_url=feed.site_url,
                favicon_url=feed.favicon_url, note=cf.note,
            )
            for cf, feed in rows
        ],
    )
