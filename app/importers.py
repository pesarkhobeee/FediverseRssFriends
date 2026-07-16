"""Shared subscription-add logic used by cherry-pick, fork, and OPML import.

All entry points are idempotent per (user, feed_url) and produce a summary
dict with counters so callers can render a single flash message.
"""

from sqlalchemy import func
from sqlmodel import Session, select

from .db import Category, Feed, Subscription, User
from .feeds import normalize_feed_url, resolve_feed


def find_or_create_category(db: Session, user: User, name: str) -> Category:
    """Case-insensitive match by name; scoped to the user."""
    name = (name or "").strip() or "Imported"
    existing = db.exec(
        select(Category)
        .where(Category.user_id == user.id)
        .where(func.lower(Category.name) == name.lower())
    ).first()
    if existing:
        return existing
    max_order = db.exec(
        select(Category.sort_order)
        .where(Category.user_id == user.id)
        .order_by(Category.sort_order.desc())
    ).first()
    cat = Category(user_id=user.id, name=name, sort_order=(max_order or 0) + 1)
    db.add(cat)
    db.flush()
    return cat


def _next_sub_order(db: Session, user_id: int, category_id: int) -> int:
    max_order = db.exec(
        select(Subscription.sort_order)
        .where(Subscription.user_id == user_id)
        .where(Subscription.category_id == category_id)
        .order_by(Subscription.sort_order.desc())
    ).first()
    return (max_order or 0) + 1


def add_subscription_to_category(
    db: Session, user: User, feed: Feed, category: Category
) -> bool:
    """Subscribe user to an existing Feed under an already-resolved Category.
    Returns True if a new Subscription row was created."""
    if db.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.feed_id == feed.id)
    ).first():
        return False
    sub = Subscription(
        user_id=user.id,
        feed_id=feed.id,
        category_id=category.id,
        sort_order=_next_sub_order(db, user.id, category.id),
    )
    db.add(sub)
    return True


def add_subscription_by_feed(
    db: Session, user: User, feed: Feed, category_name: str
) -> bool:
    """Subscribe user to an existing Feed under category_name (created if
    needed). Returns True if new."""
    cat = find_or_create_category(db, user, category_name)
    return add_subscription_to_category(db, user, feed, cat)


def add_subscription_by_url(
    db: Session,
    user: User,
    url: str,
    category_name: str,
    *,
    title_hint: str | None = None,
    site_hint: str | None = None,
    resolve_metadata: bool = False,
) -> tuple[bool, Feed]:
    """Ensure a global Feed row exists for `url`, then subscribe `user`.
    `title_hint` and `site_hint` are used to populate a NEW Feed cheaply
    (e.g. from an OPML `text=` attribute); the background fetcher will
    refresh real metadata within one cycle. Returns (added, feed)."""
    url = normalize_feed_url(url.strip())
    feed = db.exec(select(Feed).where(Feed.url == url)).first()
    if not feed:
        if resolve_metadata:
            meta = resolve_feed(url)
            feed = Feed(
                url=url,
                title=meta["title"] or title_hint or url,
                site_url=meta["site_url"] or site_hint or "",
                favicon_url=meta["favicon_url"],
            )
        else:
            from urllib.parse import urlparse
            host = urlparse(url).netloc or url
            feed = Feed(
                url=url,
                title=(title_hint or host).strip(),
                site_url=(site_hint or "").strip(),
            )
        db.add(feed)
        db.flush()
    added = add_subscription_by_feed(db, user, feed, category_name)
    return added, feed


def fork_from(db: Session, viewer: User, owner: User) -> dict:
    """Clone owner's categories+subs into viewer's radar. Returns counters."""
    subs = db.exec(
        select(Subscription, Feed, Category)
        .join(Feed, Feed.id == Subscription.feed_id)
        .join(Category, Category.id == Subscription.category_id)
        .where(Subscription.user_id == owner.id)
        .order_by(Subscription.sort_order, Subscription.id)
    ).all()
    added = 0
    skipped = 0
    for _sub, feed, cat in subs:
        if add_subscription_by_feed(db, viewer, feed, cat.name):
            added += 1
        else:
            skipped += 1
    db.commit()
    return {"added": added, "skipped": skipped, "total": len(subs)}
