"""Background feed refresh — fetch, parse, upsert items, trim history.

Called both from the APScheduler job (every REFRESH_INTERVAL_MINUTES)
and directly from tests. All errors on a single feed are captured and
recorded on the Feed row; a bad feed never breaks the cycle.
"""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from sqlmodel import Session, select

from . import config
from .db import Feed, FeedItem, _now, engine
from .feeds import USER_AGENT, _favicon_for, github_owner, resolve_avatar

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 8.0
MAX_ENTRIES_PER_FEED = 200  # cap per single fetch to keep memory bounded


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def _icon_is_reachable(url: str, timeout: float = 3.0) -> bool:
    """HEAD-probe an icon URL. Treats 2xx as healthy, everything else
    (including network errors) as broken so we heal them. False
    negatives just cost one extra deep-resolve — safe."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            r = client.head(url)
            if 200 <= r.status_code < 300:
                return True
            # Some CDNs 405/403 on HEAD but 200 on GET. Retry with GET,
            # streamed so we don't fetch the whole image body.
            if r.status_code in (403, 405):
                with client.stream("GET", url) as g:
                    return 200 <= g.status_code < 300
            return False
    except Exception:
        return False


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PUNCT_SPACE_RE = re.compile(r"\s+([.,!?;:])")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    stripped = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
    # Undo the space that tag-stripping inserted before punctuation.
    return _PUNCT_SPACE_RE.sub(r"\1", stripped)


def _derive_title(entry: Any) -> str:
    """Some feeds (notably Mastodon) omit <title>. Fall back to a short
    excerpt of the summary/content so items are recognizable."""
    raw = (entry.get("title") or "").strip()
    if raw and raw.lower() != "(untitled)":
        return raw[:500]
    body = entry.get("summary") or ""
    if not body:
        content = entry.get("content")
        if isinstance(content, list) and content:
            body = content[0].get("value", "") if isinstance(content[0], dict) else ""
    text = _strip_html(body)
    if not text:
        return "(untitled)"
    limit = 120
    return (text[:limit] + "…") if len(text) > limit else text


def _entry_guid(entry: Any) -> str:
    return (
        (entry.get("id") or "").strip()
        or (entry.get("link") or "").strip()
        or (entry.get("title") or "").strip()
    )


def _entry_published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


def fetch_one(db: Session, feed: Feed) -> tuple[int, str | None]:
    """Refresh a single feed. Returns (new_items_count, error_or_None)."""
    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            r = client.get(feed.url)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:500]
        feed.consecutive_failures += 1
        feed.last_error = msg
        feed.last_fetched_at = _now()
        db.add(feed)
        db.commit()
        log.warning("feed fetch failed url=%s err=%s", feed.url, msg)
        return 0, msg

    # Refresh feed metadata opportunistically.
    fmeta = getattr(parsed, "feed", {}) or {}
    if fmeta.get("title"):
        feed.title = fmeta["title"].strip()
    if fmeta.get("link"):
        link = fmeta["link"].strip()
        # Some feeds publish a relative site URL (e.g. <link>/</link>);
        # resolve against the feed URL so we always store an absolute
        # URL. Mirrors the same fix in resolve_feed.
        if link and not urlparse(link).netloc:
            link = urljoin(feed.url, link)
        feed.site_url = link
    # For GitHub user/org feeds, ignore the generic upstream title
    # ("Github Public timeline feed") and use the username instead —
    # otherwise every GitHub feed refresh would flip the good title
    # back to the generic one.
    owner = github_owner(feed.url)
    if owner:
        feed.title = owner
    # Re-derive the avatar every cycle so newly-supported source types
    # (GitHub owners, feeds that just started serving a channel image)
    # migrate forward. Three rules:
    #   1. Never DOWNGRADE — a healthy stored icon beats the generic
    #      /favicon.ico fallback the fast path would produce.
    #   2. If the stored icon 404s (checked via HEAD), treat it as
    #      broken and re-derive via deep resolve. This heals feeds
    #      whose <image> URL or /favicon.ico goes stale.
    #   3. If we have nothing but the generic fallback stored, try
    #      the deep path opportunistically.
    fast_candidate = resolve_avatar(feed.url, feed.site_url, fmeta)
    generic_fallback = _favicon_for(feed.site_url, feed.url)
    stored = feed.favicon_url or ""
    needs_heal = (
        not stored
        or stored == generic_fallback
        or not _icon_is_reachable(stored)
    )
    if needs_heal:
        healed = resolve_avatar(feed.url, feed.site_url, fmeta, deep=True)
        # If deep-resolve returned the same broken URL, the culprit is
        # the RSS feed's own <image> tag (e.g. Google Cloud Blog: their
        # RSS <image> URL is stale but wins in resolve_avatar before
        # the HTML scrape). Retry without feed_meta so we go straight
        # to <link rel="…icon…"> discovery.
        if healed == stored and stored:
            healed = resolve_avatar(feed.url, feed.site_url, None, deep=True)
        # Never REPLACE a non-empty icon with an empty one. Deep resolve
        # can return "" if the feed has no <image> and _favicon_for
        # couldn't derive an origin (e.g. broken site_url). Keeping the
        # old value beats blanking the icon.
        if healed or not stored:
            feed.favicon_url = healed
    elif fast_candidate != generic_fallback and fast_candidate != stored:
        # Upgrade only — the fast path found something specific (GitHub
        # owner PNG, feed <image>) that's different from what we have.
        feed.favicon_url = fast_candidate

    existing_guids = set(
        db.exec(select(FeedItem.guid).where(FeedItem.feed_id == feed.id)).all()
    )
    new = 0
    for entry in (parsed.entries or [])[:MAX_ENTRIES_PER_FEED]:
        guid = _entry_guid(entry)
        if not guid or guid in existing_guids:
            continue
        item = FeedItem(
            feed_id=feed.id,
            guid=guid[:512],
            title=_derive_title(entry),
            link=(entry.get("link") or "")[:2000],
            summary=(entry.get("summary") or "")[:4000],
            published_at=_entry_published(entry),
        )
        db.add(item)
        existing_guids.add(guid)
        new += 1

    feed.last_fetched_at = _now()
    feed.consecutive_failures = 0
    feed.last_error = ""
    db.add(feed)
    db.commit()

    if new:
        _trim_history(db, feed.id, config.FEED_ITEM_KEEP)
    return new, None


def _trim_history(db: Session, feed_id: int, keep: int) -> None:
    """Keep only the newest `keep` items per feed by published_at, then fetched_at."""
    ids = db.exec(
        select(FeedItem.id)
        .where(FeedItem.feed_id == feed_id)
        .order_by(
            FeedItem.published_at.desc(),
            FeedItem.fetched_at.desc(),
            FeedItem.id.desc(),
        )
    ).all()
    stale = ids[keep:]
    if not stale:
        return
    for row_id in stale:
        obj = db.get(FeedItem, row_id)
        if obj:
            db.delete(obj)
    db.commit()


def _due_feeds(db: Session, interval_minutes: int, batch: int) -> list[Feed]:
    cutoff = _now() - timedelta(minutes=interval_minutes)
    rows = db.exec(
        select(Feed)
        .where((Feed.last_fetched_at.is_(None)) | (Feed.last_fetched_at < cutoff))
        .order_by(Feed.last_fetched_at.asc().nulls_first())
        .limit(batch)
    ).all()
    return list(rows)


def _refresh_worker(feed_id: int) -> tuple[int, str | None]:
    """One feed's refresh in its own DB session — safe for thread-pool use.
    SQLite + WAL + check_same_thread=False supports many concurrent writers
    at MVP scale."""
    with Session(engine) as db:
        feed = db.get(Feed, feed_id)
        if not feed:
            return 0, "gone"
        return fetch_one(db, feed)


def run_cycle() -> dict:
    """One refresh cycle over all due feeds. Concurrency capped at
    REFRESH_CONCURRENCY threads. Safe to call from any thread."""
    with Session(engine) as db:
        due = _due_feeds(db, config.REFRESH_INTERVAL_MINUTES, config.REFRESH_BATCH_SIZE)
        due_ids = [f.id for f in due]

    total_new = 0
    errors = 0
    if due_ids:
        max_workers = max(1, min(config.REFRESH_CONCURRENCY, len(due_ids)))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for new, err in ex.map(_refresh_worker, due_ids):
                total_new += new
                if err:
                    errors += 1

    log.info(
        "refresh cycle: feeds=%d new_items=%d errors=%d", len(due_ids), total_new, errors
    )
    return {"feeds": len(due_ids), "new_items": total_new, "errors": errors}


def kick_background_refresh() -> None:
    """Fire-and-forget refresh (used after OPML import so newly-added feeds
    populate without waiting for the scheduler tick). Gated by
    ENABLE_SCHEDULER so tests / one-shot runs don't spawn stray threads."""
    if not config.ENABLE_SCHEDULER:
        return
    threading.Thread(target=run_cycle, daemon=True).start()
