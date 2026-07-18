"""Per-feed public landing page at /f/{feed_id} — shareable link that
non-users can view. Renders feed metadata, the newest items, and a
state-aware "Add to my radar" CTA:

  - signed-in + not subscribed   → button that POSTs /f/{id}/add
  - signed-in + already subscribed → "✓ In your radar" link to dashboard
  - anonymous                    → inline login form ("sign in to add")
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlmodel import Session, select

from . import activity
from .auth import current_user, login_required
from .db import Bookmark, Category, Feed, FeedItem, Subscription, User, get_session
from .importers import add_subscription_to_category, find_or_create_category
from .templating import make_templates

templates = make_templates()

router = APIRouter()

RECENT_ITEMS = 20
DEFAULT_CATEGORY_NAME = "Discovered"


def _subscription_state(db: Session, viewer: User | None, feed_id: int) -> str:
    """Return 'anonymous' | 'subscribed' | 'available'."""
    if viewer is None:
        return "anonymous"
    row = db.exec(
        select(Subscription)
        .where(Subscription.user_id == viewer.id)
        .where(Subscription.feed_id == feed_id)
    ).first()
    return "subscribed" if row else "available"


def _cta_partial(request: Request, feed: Feed, state: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_feed_cta.html", {"feed": feed, "state": state}
    )


@router.get("/f/{feed_id}", response_class=HTMLResponse)
def feed_page(
    feed_id: int,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not found")

    items = db.exec(
        select(FeedItem)
        .where(FeedItem.feed_id == feed_id)
        .order_by(FeedItem.published_at.desc().nulls_last(), FeedItem.fetched_at.desc())
        .limit(RECENT_ITEMS)
    ).all()

    # Total subscribers across the whole instance. Aggregate only —
    # doesn't identify anyone, so it's safe to include private users.
    subscriber_count = db.exec(
        select(func.count(Subscription.id))
        .where(Subscription.feed_id == feed_id)
    ).one()

    # For signed-in viewers, pre-load which of these items they've already
    # bookmarked so the bookmark icon renders in the 'on' state.
    viewer_bookmarks: set[int] = set()
    if viewer and items:
        ids = [it.id for it in items]
        viewer_bookmarks = set(
            db.exec(
                select(Bookmark.feed_item_id)
                .where(Bookmark.user_id == viewer.id)
                .where(Bookmark.feed_item_id.in_(ids))
            ).all()
        )

    return templates.TemplateResponse(
        request,
        "feed_page.html",
        {
            "user": viewer,
            "feed": feed,
            "items": items,
            "subscriber_count": subscriber_count,
            "state": _subscription_state(db, viewer, feed_id),
            "viewer_bookmarks": viewer_bookmarks,
        },
    )


@router.get("/f/{feed_id}/add-picker", response_class=HTMLResponse)
def add_picker(
    feed_id: int,
    request: Request,
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Inline category picker: user's existing categories in a dropdown
    + a text input to create a new one. Rendered into #feed-cta-{id}
    when the user clicks "Add to my radar"."""
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not found")
    categories = db.exec(
        select(Category)
        .where(Category.user_id == viewer.id)
        .order_by(Category.sort_order, Category.id)
    ).all()
    return templates.TemplateResponse(
        request,
        "_add_to_radar_picker.html",
        {"feed": feed, "categories": categories, "default_name": DEFAULT_CATEGORY_NAME},
    )


@router.post("/f/{feed_id}/add", response_class=HTMLResponse)
def add_feed_to_radar(
    feed_id: int,
    request: Request,
    category_id: int | None = Form(None),
    new_category: str = Form(""),
    # Legacy: some tests / older callers post `category=<name>`. Kept as
    # a fallback when neither of the new fields is present.
    category: str = Form(""),
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Subscribe the viewer to the feed. Category resolution order:
      1. `new_category` (trimmed) — created if not already present.
      2. `category_id` — must belong to the viewer (else 403).
      3. `category` name (legacy one-click callers).
      4. Fallback: DEFAULT_CATEGORY_NAME ("Discovered").
    Idempotent: adding a feed already on the radar just re-renders the
    'subscribed' CTA and does NOT move it between categories."""
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not found")

    new_name = (new_category or "").strip()
    if new_name:
        cat = find_or_create_category(db, viewer, new_name)
    elif category_id is not None:
        cat = db.get(Category, category_id)
        if not cat or cat.user_id != viewer.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not your category")
    else:
        cat = find_or_create_category(db, viewer, category or DEFAULT_CATEGORY_NAME)

    added = add_subscription_to_category(db, viewer, feed, cat)
    if added:
        activity.record(
            db, viewer, "subscribed", feed.title,
            subject_url=feed.site_url or feed.url,
            ref_type="feed", ref_id=feed.id,
        )
    db.commit()
    return _cta_partial(request, feed, state="subscribed")


@router.get("/f/{feed_id}/cta", response_class=HTMLResponse)
def cta_only(
    feed_id: int,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Re-render just the CTA partial. Used by Discover so a feed row's
    button flips to 'subscribed' after a click without a full reload."""
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not found")
    return _cta_partial(request, feed, _subscription_state(db, viewer, feed_id))
