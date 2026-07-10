"""Dashboard CRUD: categories + feed subscriptions.

All state-changing routes return small HTML fragments so HTMX can swap
them into the page. Full-page renders live at GET /dashboard.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .auth import login_required
from .db import Category, Feed, Subscription, User, get_session
from .feeds import resolve_feed

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")

router = APIRouter()


def _partial(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _load_radar(db: Session, user: User) -> list[dict]:
    """Return categories in order, each with its subscriptions + feed metadata."""
    categories = db.exec(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.sort_order, Category.id)
    ).all()

    subs = db.exec(
        select(Subscription, Feed)
        .join(Feed, Feed.id == Subscription.feed_id)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.sort_order, Subscription.id)
    ).all()

    by_cat: dict[int, list[dict]] = {c.id: [] for c in categories}
    for sub, feed in subs:
        by_cat.setdefault(sub.category_id, []).append({"sub": sub, "feed": feed})

    return [{"category": c, "subs": by_cat.get(c.id, [])} for c in categories]


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    return _partial(request, "dashboard.html", user=user, radar=_load_radar(db, user))


# ---------- Categories ----------


@router.post("/categories", response_class=HTMLResponse)
def create_category(
    request: Request,
    name: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    max_order = db.exec(
        select(Category.sort_order)
        .where(Category.user_id == user.id)
        .order_by(Category.sort_order.desc())
    ).first()
    cat = Category(user_id=user.id, name=name, sort_order=(max_order or 0) + 1)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return _partial(request, "_category.html", user=user, entry={"category": cat, "subs": []})


@router.delete("/categories/{cat_id}")
def delete_category(
    cat_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    cat = db.get(Category, cat_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    # Cascade the user's subscriptions in this category. Feed rows are global
    # and stay untouched.
    for sub in db.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.category_id == cat_id)
    ).all():
        db.delete(sub)
    db.flush()  # ensure sub rows are gone before the FK-owning parent
    db.delete(cat)
    db.commit()
    # HTMX will hx-swap the target with the empty body.
    return Response(status_code=200)


# ---------- Subscriptions ----------


@router.post("/feeds", response_class=HTMLResponse)
def subscribe(
    request: Request,
    url: str = Form(...),
    category_id: int = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    url = url.strip()
    if not url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "url required")

    cat = db.get(Category, category_id)
    if not cat or cat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")

    # Global Feed row, deduped by URL.
    feed = db.exec(select(Feed).where(Feed.url == url)).first()
    if not feed:
        meta = resolve_feed(url)
        feed = Feed(
            url=url,
            title=meta["title"],
            site_url=meta["site_url"],
            favicon_url=meta["favicon_url"],
        )
        db.add(feed)
        db.commit()
        db.refresh(feed)

    # Already subscribed? (unique per user, feed)
    existing = db.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.feed_id == feed.id)
    ).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "already subscribed")

    max_order = db.exec(
        select(Subscription.sort_order)
        .where(Subscription.user_id == user.id)
        .where(Subscription.category_id == category_id)
        .order_by(Subscription.sort_order.desc())
    ).first()
    sub = Subscription(
        user_id=user.id,
        feed_id=feed.id,
        category_id=category_id,
        sort_order=(max_order or 0) + 1,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return _partial(request, "_feed_row.html", sub=sub, feed=feed)


@router.delete("/subscriptions/{sub_id}")
def unsubscribe(
    sub_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    sub = db.get(Subscription, sub_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    db.delete(sub)
    db.commit()
    return Response(status_code=200)
