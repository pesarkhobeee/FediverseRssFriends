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
from .db import Favorite, Feed, FeedItem, Subscription, User, get_session
from .importers import add_subscription_by_feed
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
    # starred so the star renders in the 'on' state where appropriate.
    viewer_faves: set[int] = set()
    if viewer and items:
        ids = [it.id for it in items]
        viewer_faves = set(
            db.exec(
                select(Favorite.feed_item_id)
                .where(Favorite.user_id == viewer.id)
                .where(Favorite.feed_item_id.in_(ids))
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
            "viewer_faves": viewer_faves,
        },
    )


@router.post("/f/{feed_id}/add", response_class=HTMLResponse)
def add_feed_to_radar(
    feed_id: int,
    request: Request,
    category: str = Form(DEFAULT_CATEGORY_NAME),
    viewer: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """One-click add — used by /f/{id} and by Discover. Auto-creates a
    "Discovered" category the first time. Idempotent: a duplicate add
    just re-renders the 'subscribed' CTA."""
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not found")

    added = add_subscription_by_feed(db, viewer, feed, category or DEFAULT_CATEGORY_NAME)
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
