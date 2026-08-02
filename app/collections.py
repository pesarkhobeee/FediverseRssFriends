"""Community-curated feed collections (0.10.0).

Solves the RSS-discovery-is-all-English/BBC-CNN problem. Anyone can
browse a curated list of feeds around a topic + place + language, hit
"Add all to my radar", and get themselves a working reading list for
their locale in one click.

MVP scope:
  - Read: /collections (list) + /collections/{slug} (detail)
  - Write: POST /collections/{slug}/add/{feed_id} (single),
    POST /collections/{slug}/add-all (bulk)
  - Community: POST /collections/{slug}/suggest (user submits feed)
  - Moderation: GET/POST under /collections/{slug}/manage,
    curator-only.

`is_official` collections show as cards on /discover; unofficial
ones only show on /collections. `curator_id` NULL means system-curated
(only admin can moderate); non-null means that user curates.

Post-MVP (v0.11+): user-created collections, upvoting, cross-instance
federation via ActivityPub (see docs/federation-activitypub.md).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from .auth import current_user, login_required
from .db import (
    Collection,
    CollectionFeed,
    CollectionSuggestion,
    Feed,
    Subscription,
    User,
    get_session,
)
from .importers import add_subscription_to_category, find_or_create_category
from .templating import make_templates

templates = make_templates()

router = APIRouter()


# ─── Helpers ────────────────────────────────────────────────────────────


def _load_collection(db: Session, slug: str) -> Collection:
    col = db.exec(select(Collection).where(Collection.slug == slug)).first()
    if not col or not col.is_public:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collection not found")
    return col


def _load_collection_feeds(db: Session, collection_id: int) -> list[dict]:
    """Return [{feed, note, sort_order}] ordered by sort_order."""
    rows = db.exec(
        select(CollectionFeed, Feed)
        .join(Feed, Feed.id == CollectionFeed.feed_id)
        .where(CollectionFeed.collection_id == collection_id)
        .order_by(CollectionFeed.sort_order, CollectionFeed.id)
    ).all()
    return [
        {"cf": cf, "feed": feed, "note": cf.note, "sort_order": cf.sort_order}
        for cf, feed in rows
    ]


def _can_moderate(db: Session, user: User | None, col: Collection) -> bool:
    """A user can moderate a collection if they are its curator OR
    (for system-curated collections with curator_id NULL) they are the
    first-registered user on the instance (de-facto root admin). MVP
    heuristic; will be replaced by config-driven admin list."""
    if not user:
        return False
    if col.curator_id is not None:
        return user.id == col.curator_id
    root = db.exec(select(User).order_by(User.id).limit(1)).first()
    return bool(root and root.id == user.id)


def _viewer_subs(db: Session, viewer: User | None, feed_ids: list[int]) -> set[int]:
    if not viewer or not feed_ids:
        return set()
    return set(db.exec(
        select(Subscription.feed_id)
        .where(Subscription.user_id == viewer.id)
        .where(Subscription.feed_id.in_(feed_ids))
    ).all())


# ─── Read routes ───────────────────────────────────────────────────────


@router.get("/collections", response_class=HTMLResponse)
def collections_list(
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Browse all public collections. Official ones first, then the rest."""
    cols = db.exec(
        select(Collection)
        .where(Collection.is_public == True)  # noqa: E712
        .order_by(Collection.is_official.desc(), Collection.created_at.desc())
    ).all()
    # Feed counts per collection.
    counts: dict[int, int] = {}
    for c in cols:
        counts[c.id] = len(db.exec(
            select(CollectionFeed).where(CollectionFeed.collection_id == c.id)
        ).all())
    return templates.TemplateResponse(
        request, "collections_list.html",
        {"user": viewer, "collections": cols, "counts": counts},
    )


@router.get("/collections/{slug}", response_class=HTMLResponse)
def collection_detail(
    slug: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    col = _load_collection(db, slug)
    entries = _load_collection_feeds(db, col.id)
    viewer_subs = _viewer_subs(db, viewer, [e["feed"].id for e in entries])
    curator = db.get(User, col.curator_id) if col.curator_id else None
    can_moderate = _can_moderate(db, viewer, col)
    pending_n = 0
    if can_moderate:
        pending_n = len(db.exec(
            select(CollectionSuggestion)
            .where(CollectionSuggestion.collection_id == col.id)
            .where(CollectionSuggestion.status == "pending")
        ).all())
    return templates.TemplateResponse(
        request, "collection_detail.html",
        {
            "user": viewer,
            "collection": col,
            "entries": entries,
            "viewer_subs": viewer_subs,
            "curator": curator,
            "can_moderate": can_moderate,
            "pending_suggestions_count": pending_n,
        },
    )


# ─── Write routes ──────────────────────────────────────────────────────


@router.post("/collections/{slug}/add/{feed_id}", response_class=HTMLResponse)
def add_one_feed(
    slug: str,
    feed_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Add a single feed from the collection to the user's radar,
    dropped into a category named after the collection."""
    col = _load_collection(db, slug)
    # The feed must actually be in this collection — prevents anyone
    # from POSTing arbitrary feed_ids under a public collection URL.
    cf = db.exec(
        select(CollectionFeed)
        .where(CollectionFeed.collection_id == col.id)
        .where(CollectionFeed.feed_id == feed_id)
    ).first()
    if not cf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not in collection")
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed missing")
    target_cat = find_or_create_category(db, user, col.name)
    added = add_subscription_to_category(db, user, feed, target_cat)
    db.commit()

    # Re-render the row with updated state — the "Add" pill becomes "✓ On your radar".
    return templates.TemplateResponse(
        request, "_collection_feed_row.html",
        {
            "user": user,
            "collection": col,
            "e": {"cf": cf, "feed": feed, "note": cf.note, "sort_order": cf.sort_order},
            "viewer_subs": {feed.id},
            "just_added": bool(added),
        },
    )


@router.post("/collections/{slug}/add-all", response_class=HTMLResponse)
def add_all_feeds(
    slug: str,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Bulk-add every feed in the collection to the viewer's radar.
    Auto-creates a category named after the collection. Idempotent —
    already-subscribed feeds are skipped, count reported truthfully."""
    col = _load_collection(db, slug)
    target_cat = find_or_create_category(db, user, col.name)
    entries = _load_collection_feeds(db, col.id)
    added = 0
    for e in entries:
        if add_subscription_to_category(db, user, e["feed"], target_cat):
            added += 1
    db.commit()
    total = len(entries)
    already = total - added

    # HTMX-swap the "Add all" region with a confirmation banner.
    return templates.TemplateResponse(
        request, "_collection_add_all_result.html",
        {
            "user": user, "collection": col,
            "added": added, "already": already, "total": total,
        },
    )


# ─── Community suggestions ─────────────────────────────────────────────


@router.post("/collections/{slug}/suggest", response_class=HTMLResponse)
def suggest_feed(
    slug: str,
    request: Request,
    feed_url: str = Form(...),
    note: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """User submits a feed URL for a curator to review. MVP requires
    login (rate-limiting anon input is a full other issue). One user
    can't submit the same URL twice to the same collection."""
    col = _load_collection(db, slug)
    url = (feed_url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL must start with http:// or https://")

    dup = db.exec(
        select(CollectionSuggestion)
        .where(CollectionSuggestion.collection_id == col.id)
        .where(CollectionSuggestion.feed_url == url)
        .where(CollectionSuggestion.suggested_by == user.id)
        .where(CollectionSuggestion.status == "pending")
    ).first()
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT, "you've already suggested this URL")

    db.add(CollectionSuggestion(
        collection_id=col.id,
        feed_url=url,
        note=(note or "").strip()[:500],
        suggested_by=user.id,
        status="pending",
        created_at=datetime.now(UTC),
    ))
    db.commit()
    return templates.TemplateResponse(
        request, "_collection_suggest_thanks.html",
        {"user": user, "collection": col, "url": url},
    )


# ─── Curator moderation ───────────────────────────────────────────────


@router.get("/collections/{slug}/manage", response_class=HTMLResponse)
def manage_collection(
    slug: str,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    col = _load_collection(db, slug)
    if not _can_moderate(db, user, col):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not the curator")
    pending = db.exec(
        select(CollectionSuggestion, User)
        .join(User, User.id == CollectionSuggestion.suggested_by, isouter=True)
        .where(CollectionSuggestion.collection_id == col.id)
        .where(CollectionSuggestion.status == "pending")
        .order_by(CollectionSuggestion.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request, "collection_manage.html",
        {
            "user": user, "collection": col,
            "pending": [{"sug": s, "by": u} for s, u in pending],
        },
    )


@router.post("/collections/{slug}/suggestions/{sug_id}/accept")
def accept_suggestion(
    slug: str,
    sug_id: int,
    request: Request,
    note: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Curator accepts a pending suggestion. Resolves the URL through
    the same feeds.py pipeline every other subscribe path uses, so we
    validate + normalize before committing anything to the collection."""
    from .feeds import normalize_feed_url, resolve_feed

    col = _load_collection(db, slug)
    if not _can_moderate(db, user, col):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not the curator")
    sug = db.get(CollectionSuggestion, sug_id)
    if not sug or sug.collection_id != col.id or sug.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "suggestion not found")

    url = normalize_feed_url(sug.feed_url)
    feed = db.exec(select(Feed).where(Feed.url == url)).first()
    if not feed:
        meta = resolve_feed(url)
        if not meta.get("is_feed", False):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"URL didn't resolve to a feed: {url}",
            )
        feed = Feed(
            url=url, title=meta["title"],
            site_url=meta.get("site_url", ""),
            favicon_url=meta.get("favicon_url", ""),
        )
        db.add(feed)
        db.flush()

    # Dedupe: already in collection → just mark accepted.
    existing = db.exec(
        select(CollectionFeed)
        .where(CollectionFeed.collection_id == col.id)
        .where(CollectionFeed.feed_id == feed.id)
    ).first()
    if not existing:
        max_order = db.exec(
            select(CollectionFeed.sort_order)
            .where(CollectionFeed.collection_id == col.id)
            .order_by(CollectionFeed.sort_order.desc())
        ).first() or 0
        db.add(CollectionFeed(
            collection_id=col.id, feed_id=feed.id,
            note=(note or sug.note or "").strip()[:500],
            sort_order=max_order + 1,
        ))
    sug.status = "accepted"
    sug.reviewed_at = datetime.now(UTC)
    db.add(sug)
    db.commit()
    return RedirectResponse(f"/collections/{slug}/manage", status_code=303)


@router.post("/collections/{slug}/suggestions/{sug_id}/reject")
def reject_suggestion(
    slug: str,
    sug_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    col = _load_collection(db, slug)
    if not _can_moderate(db, user, col):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not the curator")
    sug = db.get(CollectionSuggestion, sug_id)
    if not sug or sug.collection_id != col.id or sug.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "suggestion not found")
    sug.status = "rejected"
    sug.reviewed_at = datetime.now(UTC)
    db.add(sug)
    db.commit()
    return RedirectResponse(f"/collections/{slug}/manage", status_code=303)


# ─── Discover helper — used from public.py's discover route ────────────


def load_featured_collections(db: Session, limit: int = 12) -> list[dict]:
    """Return official public collections + feed counts, for the
    /discover page's 'Browse collections' band."""
    cols = db.exec(
        select(Collection)
        .where(Collection.is_public == True)   # noqa: E712
        .where(Collection.is_official == True)  # noqa: E712
        .order_by(Collection.created_at.desc())
        .limit(limit)
    ).all()
    out = []
    for c in cols:
        n = len(db.exec(
            select(CollectionFeed).where(CollectionFeed.collection_id == c.id)
        ).all())
        out.append({"col": c, "feed_count": n})
    return out
