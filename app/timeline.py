"""Home timeline at /feed — signed-in users' view of what the people
they follow have been doing: starring items, subscribing to feeds,
following others, forking radars.

Previously this also merged fresh items from the viewer's own
subscribed feeds, but that firehose drowned the social signal —
subscribed-item browsing lives on /u/{handle} and /dashboard.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlmodel import Session, select

from . import activity
from .auth import login_required
from .db import (
    Activity,
    Category,
    CategoryFollow,
    Feed,
    FeedItem,
    Follow,
    RemoteActor,
    RemoteFollowing,
    RemoteItem,
    Subscription,
    User,
    get_session,
)
from .templating import make_templates

templates = make_templates()

router = APIRouter()

FEED_WINDOW_DAYS = 30
FEED_LIMIT = 100
# 0.11.42: page size for cursor pagination on /feed. Small enough that
# each page loads quickly; large enough that active users don't feel
# the need to paginate constantly.
FEED_PAGE_SIZE = 25


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _load_timeline(
    db: Session, user: User, before: datetime | None = None
) -> dict:
    cutoff = datetime.now(UTC) - timedelta(days=FEED_WINDOW_DAYS)

    # Who am I following (approved only)?
    followee_ids = list(
        db.exec(
            select(Follow.followee_id)
            .where(Follow.follower_id == user.id)
            .where(Follow.approved_at.is_not(None))
        ).all()
    )
    handle_by_id: dict[int, User] = {}
    if followee_ids:
        users = db.exec(select(User).where(User.id.in_(followee_ids))).all()
        handle_by_id = {u.id: u for u in users}

    seen_activity_ids: set[int] = set()
    entries: list[dict] = []
    if followee_ids:
        q = (
            select(Activity)
            .where(Activity.user_id.in_(followee_ids))
            .where(Activity.created_at >= cutoff)
        )
        if before is not None:
            q = q.where(Activity.created_at < before)
        rows = db.exec(
            q.order_by(Activity.created_at.desc()).limit(FEED_LIMIT)
        ).all()
        for ev in rows:
            seen_activity_ids.add(ev.id)
            entries.append({
                "kind": "activity",
                "ts": _aware(ev.created_at),
                "actor": handle_by_id.get(ev.user_id),
                "activity": ev,
            })

    # Category-level follows: pull item-scoped activity (subscribed +
    # bookmarked) whose referenced feed lives in a Subscription of that
    # category on the followee's own radar. Person-scoped kinds
    # (followed/forked/created_category/posted) don't fit a category
    # scope and only reach the viewer via whole-user Follow.
    cat_follow_rows = db.exec(
        select(CategoryFollow, Category)
        .join(Category, Category.id == CategoryFollow.category_id)
        .where(CategoryFollow.follower_id == user.id)
    ).all()
    n_cat_follows = len(cat_follow_rows)
    for _cf, cat in cat_follow_rows:
        # Feed ids in that category, on the category owner's radar.
        feed_ids = list(db.exec(
            select(Subscription.feed_id)
            .where(Subscription.user_id == cat.user_id)
            .where(Subscription.category_id == cat.id)
        ).all())
        if not feed_ids:
            continue
        # Owner's item-scoped activity referencing any of those feeds.
        # For bookmarked (ref_type='item'), ref_id is a FeedItem — we need
        # to filter by item.feed_id. For subscribed (ref_type='feed'),
        # ref_id IS the feed_id.
        subs_q = (
            select(Activity)
            .where(Activity.user_id == cat.user_id)
            .where(Activity.kind == "subscribed")
            .where(Activity.ref_type == "feed")
            .where(Activity.ref_id.in_(feed_ids))
            .where(Activity.created_at >= cutoff)
        )
        fav_q = (
            select(Activity)
            .join(FeedItem, FeedItem.id == Activity.ref_id)
            .where(Activity.user_id == cat.user_id)
            .where(Activity.kind == "bookmarked")
            .where(Activity.ref_type == "item")
            .where(FeedItem.feed_id.in_(feed_ids))
            .where(Activity.created_at >= cutoff)
        )
        if before is not None:
            subs_q = subs_q.where(Activity.created_at < before)
            fav_q = fav_q.where(Activity.created_at < before)
        subs_rows = db.exec(
            subs_q.order_by(Activity.created_at.desc()).limit(FEED_LIMIT)
        ).all()
        fav_rows = db.exec(
            fav_q.order_by(Activity.created_at.desc()).limit(FEED_LIMIT)
        ).all()
        actor = handle_by_id.get(cat.user_id)
        if actor is None:
            actor = db.get(User, cat.user_id)
            if actor:
                handle_by_id[cat.user_id] = actor
        for ev in list(subs_rows) + list(fav_rows):
            if ev.id in seen_activity_ids:
                continue
            seen_activity_ids.add(ev.id)
            entries.append({
                "kind": "activity",
                "ts": _aware(ev.created_at),
                "actor": actor,
                "activity": ev,
            })

    # 0.11.30 (M10): mix in Notes from remote actors we follow via
    # ActivityPub. RemoteItem rows are populated by the inbox handler
    # when a Mastodon (etc.) account we follow toots.
    remote_q = (
        select(RemoteItem, RemoteActor)
        .join(RemoteActor, RemoteActor.id == RemoteItem.remote_actor_id)
        .join(RemoteFollowing,
              RemoteFollowing.remote_actor_id == RemoteActor.id)
        .where(RemoteFollowing.user_id == user.id)
        .where(RemoteFollowing.accepted_at.is_not(None))
        .where(RemoteItem.published_at >= cutoff)
    )
    if before is not None:
        remote_q = remote_q.where(RemoteItem.published_at < before)
    remote_rows = db.exec(
        remote_q.order_by(RemoteItem.published_at.desc()).limit(FEED_LIMIT)
    ).all()
    for it, ra in remote_rows:
        entries.append({
            "kind": "remote_note",
            "ts": _aware(it.published_at),
            "actor": None,          # no local User for remote actors
            "remote_actor": ra,
            "note": it,
        })

    entries.sort(key=lambda e: e["ts"], reverse=True)

    # Enrich entries with feed + "already on my radar?" state so the
    # template can render an "Add to my radar" pill for feeds the
    # viewer doesn't yet follow. Only activity that unambiguously
    # references a Feed gets a pill:
    #   - subscribed  (ref_type=feed)         → ref_id IS a feed_id
    #   - bookmarked  (ref_type=item)         → item.feed_id
    #   - posted      (ref_type=external_item) → item.feed_id
    # followed / forked / created_category don't map to a single feed.
    feed_ids_needed: set[int] = set()
    item_ids_needed: set[int] = set()
    for e in entries:
        # 0.11.33: remote_note entries (M10 inbound AP Notes) don't
        # carry a local Activity — skip enrichment for them.
        if e.get("kind") == "remote_note":
            continue
        ev = e["activity"]
        if ev.ref_type == "feed" and ev.ref_id is not None:
            feed_ids_needed.add(ev.ref_id)
        elif ev.ref_type in ("item", "external_item") and ev.ref_id is not None:
            item_ids_needed.add(ev.ref_id)

    # item_id → feed_id resolution in one query.
    item_feed: dict[int, int] = {}
    if item_ids_needed:
        rows = db.exec(
            select(FeedItem.id, FeedItem.feed_id)
            .where(FeedItem.id.in_(item_ids_needed))
        ).all()
        item_feed = {i: f for i, f in rows}
        feed_ids_needed.update(item_feed.values())

    # Load feeds + viewer's existing subscriptions in bulk.
    feed_by_id: dict[int, Feed] = {}
    already_subscribed: set[int] = set()
    # 0.11.2: viewer's private per-feed note (feed_id → note text) so
    # the timeline byline can show "@dhh — creator of Rails" for feeds
    # the reader has personally annotated.
    viewer_note_by_feed: dict[int, str] = {}
    if feed_ids_needed:
        feeds = db.exec(select(Feed).where(Feed.id.in_(feed_ids_needed))).all()
        feed_by_id = {f.id: f for f in feeds}
        my_subs = db.exec(
            select(Subscription.feed_id, Subscription.note)
            .where(Subscription.user_id == user.id)
            .where(Subscription.feed_id.in_(feed_ids_needed))
        ).all()
        already_subscribed = {fid for fid, _ in my_subs}
        viewer_note_by_feed = {fid: n for fid, n in my_subs if n}

    for e in entries:
        if e.get("kind") == "remote_note":
            continue
        ev = e["activity"]
        feed_id: int | None = None
        if ev.ref_type == "feed" and ev.ref_id is not None:
            feed_id = ev.ref_id
        elif ev.ref_type in ("item", "external_item") and ev.ref_id is not None:
            feed_id = item_feed.get(ev.ref_id)
        # Attach if we could map to a real Feed row. Skip if the actor
        # IS the viewer (no point offering "add to my radar" for your
        # own activity).
        if feed_id and feed_id in feed_by_id:
            actor_is_viewer = e["actor"] is not None and e["actor"].id == user.id
            if not actor_is_viewer:
                e["feed"] = feed_by_id[feed_id]
                e["cta_state"] = "subscribed" if feed_id in already_subscribed else "available"
                if feed_id in viewer_note_by_feed:
                    e["viewer_note"] = viewer_note_by_feed[feed_id]

    return {
        "entries": entries,
        "n_followees": len(followee_ids),
        "n_category_follows": n_cat_follows,
        "kind_icon": activity.KIND_ICON,
        "kind_verb": activity.KIND_VERB,
    }


# 0.11.41: Friends-timeline source filter. Values are the exact
# strings we accept in ?src= and render in the chip row.
FEED_SRC_VALUES = ("all", "local", "fediverse")


def _source_totals(db: Session, user: User, cutoff: datetime) -> tuple[int, int]:
    """Real per-bucket totals over the 30-day window.

    Prior to 0.11.45 the chip badges reflected the paginated top-slice
    (capped at FEED_LIMIT), which under-counted local activity when
    the fediverse bucket was dominant enough to crowd it out of the
    merged window. These are separate COUNT queries so the badges are
    an honest signal — "how much your radar actually has to offer",
    not "how much made it onto this page". """
    # Local: distinct Activity.id across direct Follow + CategoryFollow
    # sources. Collect ids so dedup between the two paths is exact.
    followee_ids = list(db.exec(
        select(Follow.followee_id)
        .where(Follow.follower_id == user.id)
        .where(Follow.approved_at.is_not(None))
    ).all())
    ids: set[int] = set()
    if followee_ids:
        ids.update(db.exec(
            select(Activity.id)
            .where(Activity.user_id.in_(followee_ids))
            .where(Activity.created_at >= cutoff)
        ).all())
    cat_follow_rows = db.exec(
        select(CategoryFollow, Category)
        .join(Category, Category.id == CategoryFollow.category_id)
        .where(CategoryFollow.follower_id == user.id)
    ).all()
    for _cf, cat in cat_follow_rows:
        feed_ids = list(db.exec(
            select(Subscription.feed_id)
            .where(Subscription.user_id == cat.user_id)
            .where(Subscription.category_id == cat.id)
        ).all())
        if not feed_ids:
            continue
        ids.update(db.exec(
            select(Activity.id)
            .where(Activity.user_id == cat.user_id)
            .where(Activity.kind == "subscribed")
            .where(Activity.ref_type == "feed")
            .where(Activity.ref_id.in_(feed_ids))
            .where(Activity.created_at >= cutoff)
        ).all())
        ids.update(db.exec(
            select(Activity.id)
            .join(FeedItem, FeedItem.id == Activity.ref_id)
            .where(Activity.user_id == cat.user_id)
            .where(Activity.kind == "bookmarked")
            .where(Activity.ref_type == "item")
            .where(FeedItem.feed_id.in_(feed_ids))
            .where(Activity.created_at >= cutoff)
        ).all())
    n_local = len(ids)

    n_fedi = db.exec(
        select(func.count(RemoteItem.id))
        .join(RemoteActor, RemoteActor.id == RemoteItem.remote_actor_id)
        .join(RemoteFollowing, RemoteFollowing.remote_actor_id == RemoteActor.id)
        .where(RemoteFollowing.user_id == user.id)
        .where(RemoteFollowing.accepted_at.is_not(None))
        .where(RemoteItem.published_at >= cutoff)
    ).one() or 0
    return n_local, n_fedi


def _parse_before(raw: str | None) -> datetime | None:
    """Parse the ?before= cursor (ISO-8601, produced by us). Silently
    drops garbage — the worst outcome is a user seeing page 1 again."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@router.get("/feed", response_class=HTMLResponse)
def home_timeline(
    request: Request,
    src: str = "all",
    before: str | None = None,
    partial: int = 0,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    before_dt = _parse_before(before)
    ctx = _load_timeline(db, user, before=before_dt)
    # 0.11.45: chip-badge counts are REAL bucket totals over the 30-day
    # window (via _source_totals), not the size of the loaded slice.
    # Prior versions counted `all_entries` after the load — which was
    # capped at FEED_LIMIT and so under-counted whichever source lost
    # the timestamp race in the merge.
    cutoff = datetime.now(UTC) - timedelta(days=FEED_WINDOW_DAYS)
    n_local, n_fedi = _source_totals(db, user, cutoff)
    all_entries = ctx["entries"]

    src = src if src in FEED_SRC_VALUES else "all"
    if src == "local":
        filtered = [e for e in all_entries if e.get("kind") != "remote_note"]
    elif src == "fediverse":
        filtered = [e for e in all_entries if e.get("kind") == "remote_note"]
    else:
        filtered = all_entries

    # Slice to a page. If we have MORE than a page's worth of matching
    # entries, expose a cursor pointing at the last kept entry's ts —
    # the next request will ask for entries strictly older than that.
    page = filtered[:FEED_PAGE_SIZE]
    next_before = None
    if len(filtered) > FEED_PAGE_SIZE:
        next_before = page[-1]["ts"].isoformat()

    ctx["entries"] = page
    ctx["user"] = user
    ctx["feed_src"] = src
    ctx["feed_src_counts"] = {
        "all": n_local + n_fedi,
        "local": n_local,
        "fediverse": n_fedi,
    }
    ctx["feed_next_before"] = next_before
    ctx["feed_before"] = before if before_dt is not None else None
    # 0.11.43: ?partial=1 returns just the entries-and-load-more
    # fragment, used by the HTMX "Load more" button to append inline.
    template = "_timeline_page.html" if partial else "timeline.html"
    return templates.TemplateResponse(request, template, ctx)
