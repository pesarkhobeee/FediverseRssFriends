"""Read-only public JSON API, mounted at /api/v1 (docs/public-api.md).

Anonymous by design: nothing here reads the session, so every caller
gets the same response and a CDN may cache it. Every handle goes
through `can_view(None, …)`; private users are 404, same as unknown.
Responses are built from explicit models, never from table rows, so a
new column on `User` or `Subscription` can't leak by accident.
"""

from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, func, select

from . import __version__, config
from .db import Category, Feed, Subscription, User, get_session
from .public import can_view, resolve_handle

API_VERSION = "1"

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
        if not db.exec(
            select(Category.id).where(Category.user_id == owner.id, Category.name == category)
        ).first():
            raise HTTPException(404, "Category not found")
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
