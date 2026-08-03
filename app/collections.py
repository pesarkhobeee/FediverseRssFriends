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

import re
from datetime import UTC, datetime
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlmodel import Session, select

from . import config, mail
from .auth import current_user, login_required
from .db import (
    Collection,
    CollectionFeed,
    CollectionFeedTombstone,
    CollectionRequest,
    CollectionSuggestion,
    Feed,
    FeedItem,
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That collection doesn't exist.")
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


def _is_admin(db: Session, user: User | None) -> bool:
    """Instance-level admin check. Two paths:

      1. `config.ADMIN_HANDLES` non-empty → user is in that list.
      2. `config.ADMIN_HANDLES` empty (default) → the first-registered
         user on the instance is the de-facto root admin. Zero-config
         path for fresh instances; also matches how _can_moderate has
         handled system-curated collections since 0.10.0.
    """
    if not user:
        return False
    if config.ADMIN_HANDLES:
        return user.handle in config.ADMIN_HANDLES
    root = db.exec(select(User).order_by(User.id).limit(1)).first()
    return bool(root and root.id == user.id)


def _can_moderate(db: Session, user: User | None, col: Collection) -> bool:
    """A user can moderate a collection if they are its curator OR
    an instance admin (see `_is_admin`)."""
    if not user:
        return False
    if col.curator_id is not None and user.id == col.curator_id:
        return True
    return _is_admin(db, user)


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
    lang: str = Query(default=""),
    viewer: User | None = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Browse all public collections. Official first, then the rest.

    `?lang=xx` filters to collections declaring that language (ISO 639-1).
    """
    # Compute language chips from ALL public collections so the pill row
    # is stable regardless of the current filter.
    all_langs = sorted(set(db.exec(
        select(Collection.language)
        .where(Collection.is_public == True)   # noqa: E712
        .where(Collection.language != "")
    ).all()))

    query = (
        select(Collection)
        .where(Collection.is_public == True)  # noqa: E712
        .order_by(Collection.is_official.desc(), Collection.created_at.desc())
    )
    if lang:
        query = query.where(Collection.language == lang)
    cols = db.exec(query).all()

    # Feed counts per collection.
    counts: dict[int, int] = {}
    for c in cols:
        counts[c.id] = len(db.exec(
            select(CollectionFeed).where(CollectionFeed.collection_id == c.id)
        ).all())
    return templates.TemplateResponse(
        request, "collections_list.html",
        {
            "user": viewer,
            "collections": cols,
            "counts": counts,
            "active_lang": lang,
            "all_langs": all_langs,
            "is_admin": _is_admin(db, viewer),
        },
    )


# ─── Anyone (signed-in): request a whole new collection ───────────────


@router.get("/collections/request", response_class=HTMLResponse)
def request_collection_form(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """0.11.4: a signed-in user asks for a new collection to exist.
    Not the same as suggesting a feed for an existing collection —
    this is 'please make a collection for Amsterdam news'.
    Admins act on requests by creating the collection via /collections/new."""
    my_recent = db.exec(
        select(CollectionRequest)
        .where(CollectionRequest.requested_by == user.id)
        .order_by(CollectionRequest.created_at.desc())
        .limit(10)
    ).all()
    return templates.TemplateResponse(
        request, "collection_request.html",
        {"user": user, "my_recent": my_recent},
    )


@router.post("/collections/request")
def request_collection(
    name: str = Form(...),
    description: str = Form(""),
    language: str = Form(""),
    locale: str = Form(""),
    example_feeds: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required.")
    # Cheap dedup: same user can't file two identical open requests.
    dupe = db.exec(
        select(CollectionRequest)
        .where(CollectionRequest.requested_by == user.id)
        .where(CollectionRequest.name == name)
        .where(CollectionRequest.status == "pending")
    ).first()
    if dupe:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have a pending request with that name — hang tight until it's reviewed.",
        )
    req = CollectionRequest(
        requested_by=user.id,
        name=name[:120],
        description=(description or "").strip()[:600],
        language=(language or "").strip().lower()[:5],
        locale=(locale or "").strip()[:64],
        example_feeds=(example_feeds or "").strip()[:2000],
    )
    db.add(req)
    db.commit()
    import contextlib
    with contextlib.suppress(Exception):
        # Never let a broken SMTP break the user's request.
        mail.send_collection_request_notification(
            name=req.name,
            description=req.description,
            language=req.language,
            locale=req.locale,
            example_feeds=req.example_feeds,
            requester_handle=user.handle,
        )
    return RedirectResponse("/collections/request?ok=1", status_code=303)


# ─── Admin: create / edit collections + manually add feeds ─────────────
#
# All admin routes registered BEFORE the plain `/collections/{slug}`
# matcher so FastAPI can't accidentally treat "new" as a slug.


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


@router.get("/collections/new", response_class=HTMLResponse)
def new_collection_form(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Admin-only. Renders the 'create a new collection' form."""
    if not _is_admin(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This is admin-only.")
    return templates.TemplateResponse(
        request, "collection_new.html", {"user": user},
    )


@router.post("/collections/new")
def create_collection(
    slug: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    language: str = Form(""),
    locale: str = Form(""),
    is_official: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Admin-only. Creates the Collection row + redirects to the new
    detail page. The curator is the creating admin (nullable = system
    only when created by the seed loop)."""
    if not _is_admin(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This is admin-only.")

    slug = (slug or "").strip().lower()
    name = (name or "").strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Slug must be 2–64 characters: lowercase letters, digits, "
            "and hyphens, starting with a letter.",
        )
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required.")
    if db.exec(select(Collection).where(Collection.slug == slug)).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A collection with the slug '{slug}' already exists — pick another.",
        )

    col = Collection(
        slug=slug,
        name=name,
        description=(description or "").strip(),
        language=(language or "").strip().lower()[:5],
        locale=(locale or "").strip()[:64],
        curator_id=user.id,           # creator is the curator
        is_official=(is_official == "on"),
        is_public=True,
        created_at=datetime.now(UTC),
    )
    db.add(col)
    db.commit()
    return RedirectResponse(f"/collections/{slug}", status_code=303)


@router.get("/collections/{slug}/edit", response_class=HTMLResponse)
def edit_collection_form(
    slug: str,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    col = _load_collection(db, slug)
    if not _can_moderate(db, user, col):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't moderate this collection — only its curator can.",
        )
    return templates.TemplateResponse(
        request, "collection_edit.html",
        {"user": user, "collection": col, "is_admin": _is_admin(db, user)},
    )


@router.post("/collections/{slug}/edit")
def edit_collection(
    slug: str,
    name: str = Form(...),
    description: str = Form(""),
    language: str = Form(""),
    locale: str = Form(""),
    is_official: str = Form(""),
    is_public: str = Form("on"),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Curator or admin updates the collection's metadata (not the
    slug — the slug is the URL and stable). is_official + is_public
    are checkbox-style: present = True, absent = False."""
    col = _load_collection(db, slug)
    if not _can_moderate(db, user, col):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't moderate this collection — only its curator can.",
        )

    name = (name or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required.")

    col.name = name
    col.description = (description or "").strip()
    col.language = (language or "").strip().lower()[:5]
    col.locale = (locale or "").strip()[:64]
    # Only admins can flip is_official (curators of a personal collection
    # can't self-promote it to the featured band).
    if _is_admin(db, user):
        col.is_official = (is_official == "on")
    col.is_public = (is_public == "on")
    db.add(col)
    db.commit()
    return RedirectResponse(f"/collections/{slug}", status_code=303)


@router.post("/collections/{slug}/add-feed")
def curator_add_feed(
    slug: str,
    feed_url: str = Form(...),
    note: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Curator adds a feed to a collection directly, skipping the
    suggestion queue. Runs through the standard normalize +
    resolve_feed pipeline so we validate before committing anything."""
    from .feeds import normalize_feed_url, resolve_feed

    col = _load_collection(db, slug)
    if not _can_moderate(db, user, col):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't moderate this collection — only its curator can.",
        )

    url = normalize_feed_url((feed_url or "").strip())
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "URL must start with http:// or https://"
        )

    feed = db.exec(select(Feed).where(Feed.url == url)).first()
    if not feed:
        meta = resolve_feed(url)
        if not meta.get("is_feed", False):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"That URL didn't look like a valid feed: {url}",
            )
        feed = Feed(
            url=url, title=meta["title"],
            site_url=meta.get("site_url", ""),
            favicon_url=meta.get("favicon_url", ""),
        )
        db.add(feed)
        db.flush()

    # Dedup: already in collection → 409.
    if db.exec(
        select(CollectionFeed)
        .where(CollectionFeed.collection_id == col.id)
        .where(CollectionFeed.feed_id == feed.id)
    ).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That feed is already in this collection.",
        )

    max_order = db.exec(
        select(CollectionFeed.sort_order)
        .where(CollectionFeed.collection_id == col.id)
        .order_by(CollectionFeed.sort_order.desc())
    ).first() or 0
    db.add(CollectionFeed(
        collection_id=col.id,
        feed_id=feed.id,
        note=(note or "").strip()[:500],
        sort_order=max_order + 1,
    ))
    # 0.11.4: if this URL had been tombstoned by a prior removal, clear
    # the tombstone — curator has changed their mind and wants seed-sync
    # to keep hands off this URL going forward.
    tomb = db.exec(
        select(CollectionFeedTombstone)
        .where(CollectionFeedTombstone.collection_id == col.id)
        .where(CollectionFeedTombstone.feed_url == url)
    ).first()
    if tomb:
        db.delete(tomb)
    db.commit()
    return RedirectResponse(f"/collections/{slug}/manage", status_code=303)


# NOTE on route ordering: `/collections/{slug}.rss` must be registered
# BEFORE the plain `{slug}` route below — otherwise FastAPI would match
# "berlin-nachrichten.rss" as `slug="berlin-nachrichten.rss"` on the HTML
# endpoint and return 404.

@router.get("/collections/{slug}.rss", response_class=PlainTextResponse)
def collection_rss(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
):
    """Every collection is subscribable as a single RSS feed. Aggregates
    the newest 100 items across all feeds in the collection, sorted by
    `published_at DESC` (then `fetched_at DESC`).

    Rationale: the whole "not-just-BBC/CNN" mission only reaches users
    of other RSS readers if they can subscribe to a collection without
    having a rssFriends account. This is that URL."""
    col = _load_collection(db, slug)

    # Which feeds belong to this collection?
    feed_ids = list(db.exec(
        select(CollectionFeed.feed_id)
        .where(CollectionFeed.collection_id == col.id)
    ).all())
    if not feed_ids:
        items_rows: list = []
    else:
        items_rows = db.exec(
            select(FeedItem, Feed)
            .join(Feed, Feed.id == FeedItem.feed_id)
            .where(FeedItem.feed_id.in_(feed_ids))
            .order_by(
                FeedItem.published_at.desc().nulls_last(),
                FeedItem.fetched_at.desc(),
            )
            .limit(100)
        ).all()

    base = str(request.base_url).rstrip("/")

    def _rfc822(dt: datetime | None) -> str:
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    entries = []
    for item, feed in items_rows:
        pubdate = _rfc822(item.published_at or item.fetched_at)
        title_prefix = f"[{feed.title}] " if feed.title else ""
        parts = [
            "    <item>",
            f"      <title>{xml_escape(title_prefix + (item.title or '(untitled)'))}</title>",
            f"      <link>{xml_escape(item.link or feed.url)}</link>",
            f'      <guid isPermaLink="false">item-{item.id}</guid>',
            f'      <source url="{xml_escape(feed.url)}">{xml_escape(feed.title)}</source>',
        ]
        if pubdate:
            parts.append(f"      <pubDate>{pubdate}</pubDate>")
        if item.summary:
            parts.append(f"      <description>{xml_escape(item.summary)}</description>")
        parts.append("    </item>")
        entries.append("\n".join(parts))

    lang_attr = f' xml:lang="{xml_escape(col.language)}"' if col.language else ""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<rss version="2.0"{lang_attr}>\n'
        "  <channel>\n"
        f"    <title>{xml_escape(col.name)}</title>\n"
        f"    <link>{base}/collections/{xml_escape(col.slug)}</link>\n"
        f"    <description>{xml_escape(col.description or col.name)}</description>\n"
        + ("\n".join(entries) + "\n" if entries else "")
        + "  </channel>\n"
        "</rss>\n"
    )
    return PlainTextResponse(
        body,
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=900"},  # 15-min CDN-friendly cache
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
            "can_moderate": _can_moderate(db, user, col),
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
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You've already suggested this URL — hang tight while the curator reviews it.",
        )

    db.add(CollectionSuggestion(
        collection_id=col.id,
        feed_url=url,
        note=(note or "").strip()[:500],
        suggested_by=user.id,
        status="pending",
        created_at=datetime.now(UTC),
    ))
    db.commit()
    # Notify configured recipients. Silent no-op when disabled or
    # SUGGESTION_NOTIFY_EMAILS is empty in config. Errors don't
    # propagate — the user's action succeeds even if mail fails.
    mail.send_suggestion_notification(
        collection_slug=col.slug,
        collection_name=col.name,
        feed_url=url,
        note=(note or "").strip()[:500],
        suggester_handle=user.handle,
    )
    return templates.TemplateResponse(
        request, "_collection_suggest_thanks.html",
        {"user": user, "collection": col, "url": url},
    )


# ─── Curator: remove a feed from the collection ────────────────────────


@router.post("/collections/{slug}/remove/{feed_id}", response_class=HTMLResponse)
def remove_feed_from_collection(
    slug: str,
    feed_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Curator-only. Drops the CollectionFeed link (the Feed row itself
    stays — other users may still be subscribed). Idempotent."""
    col = _load_collection(db, slug)
    if not _can_moderate(db, user, col):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't moderate this collection — only its curator can.",
        )
    cf = db.exec(
        select(CollectionFeed)
        .where(CollectionFeed.collection_id == col.id)
        .where(CollectionFeed.feed_id == feed_id)
    ).first()
    if cf:
        # 0.11.4: tombstone the URL so the seed loop won't resurrect
        # this feed on the next deploy. Look up the Feed's URL by id
        # BEFORE deleting the CollectionFeed row (we still need the
        # Feed row itself, but this is the intent-of-removal record).
        feed = db.get(Feed, feed_id)
        if feed:
            already = db.exec(
                select(CollectionFeedTombstone)
                .where(CollectionFeedTombstone.collection_id == col.id)
                .where(CollectionFeedTombstone.feed_url == feed.url)
            ).first()
            if not already:
                db.add(CollectionFeedTombstone(
                    collection_id=col.id,
                    feed_url=feed.url,
                    removed_by=user.id,
                ))
        db.delete(cf)
        db.commit()
    # Empty HTMX response: the target row is swapped for nothing.
    return HTMLResponse("", status_code=200)


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
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't moderate this collection — only its curator can.",
        )
    pending = db.exec(
        select(CollectionSuggestion, User)
        .join(User, User.id == CollectionSuggestion.suggested_by, isouter=True)
        .where(CollectionSuggestion.collection_id == col.id)
        .where(CollectionSuggestion.status == "pending")
        .order_by(CollectionSuggestion.created_at.desc())
    ).all()
    # Also load the current feed list so the manage page can show
    # "how many + what's in there" alongside the pending queue.
    feeds = _load_collection_feeds(db, col.id)
    return templates.TemplateResponse(
        request, "collection_manage.html",
        {
            "user": user, "collection": col,
            "pending": [{"sug": s, "by": u} for s, u in pending],
            "current_feeds": feeds,
            "is_admin": _is_admin(db, user),
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
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't moderate this collection — only its curator can.",
        )
    sug = db.get(CollectionSuggestion, sug_id)
    if not sug or sug.collection_id != col.id or sug.status != "pending":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That suggestion doesn't exist — it may have already been reviewed.",
        )

    url = normalize_feed_url(sug.feed_url)
    feed = db.exec(select(Feed).where(Feed.url == url)).first()
    if not feed:
        meta = resolve_feed(url)
        if not meta.get("is_feed", False):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"That URL didn't look like a valid feed: {url}",
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
    # 0.11.4: clear any tombstone for this URL — accepting a suggestion
    # for a previously-removed feed is a positive re-add signal.
    tomb = db.exec(
        select(CollectionFeedTombstone)
        .where(CollectionFeedTombstone.collection_id == col.id)
        .where(CollectionFeedTombstone.feed_url == url)
    ).first()
    if tomb:
        db.delete(tomb)
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
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't moderate this collection — only its curator can.",
        )
    sug = db.get(CollectionSuggestion, sug_id)
    if not sug or sug.collection_id != col.id or sug.status != "pending":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That suggestion doesn't exist — it may have already been reviewed.",
        )
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
