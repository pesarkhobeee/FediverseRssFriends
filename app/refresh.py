"""Background feed refresh — fetch, parse, upsert items, trim history.

Called both from the APScheduler job (every REFRESH_INTERVAL_MINUTES)
and directly from tests. All errors on a single feed are captured and
recorded on the Feed row; a bad feed never breaks the cycle.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import httpx
from sqlmodel import Session, select

from . import config
from .db import Feed, FeedItem, _now, engine
from .feeds import USER_AGENT

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 8.0
MAX_ENTRIES_PER_FEED = 200  # cap per single fetch to keep memory bounded


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


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
        feed.site_url = fmeta["link"].strip()

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
            title=(entry.get("title") or "(untitled)")[:500],
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


def run_cycle() -> dict:
    """One refresh cycle over all due feeds. Safe to call from a scheduler thread."""
    with Session(engine) as db:
        due = _due_feeds(db, config.REFRESH_INTERVAL_MINUTES, config.REFRESH_BATCH_SIZE)
        total_new = 0
        errors = 0
        for feed in due:
            new, err = fetch_one(db, feed)
            total_new += new
            if err:
                errors += 1
    log.info("refresh cycle: feeds=%d new_items=%d errors=%d", len(due), total_new, errors)
    return {"feeds": len(due), "new_items": total_new, "errors": errors}
