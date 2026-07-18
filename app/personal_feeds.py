"""Personal feeds: external RSS/Atom feeds a user claims as their own
(their Mastodon, YouTube, personal blog, ...). New items from these
feeds emit Activity(kind='posted') for the owner, so followers see the
cross-post on their timeline.

Semantically distinct from Subscription (which means "I read this"):
personal feeds mean "I authored this", so they don't appear on the
owner's reading radar, don't count toward Discover popularity, and
aren't in OPML export.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlmodel import Session, select

from .auth import login_required
from .db import Feed, PersonalFeed, User, get_session
from .feeds import discover_feeds, normalize_feed_url, resolve_feed
from .templating import make_templates

templates = make_templates()

router = APIRouter()

MAX_PERSONAL_FEEDS_PER_USER = 20


def _partial(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _load_personal_feeds(db: Session, user: User) -> list[dict]:
    rows = db.exec(
        select(PersonalFeed, Feed)
        .join(Feed, Feed.id == PersonalFeed.feed_id)
        .where(PersonalFeed.user_id == user.id)
        .order_by(PersonalFeed.connected_at.desc())
    ).all()
    return [{"pf": pf, "feed": feed} for pf, feed in rows]


def _register_personal_feed(
    db: Session, user: User, url: str, label: str = ""
) -> PersonalFeed:
    """Ensure a global Feed row exists at `url`, then link it to the
    user as a personal feed. Idempotent per (user, feed)."""
    if db.exec(
        select(PersonalFeed).where(PersonalFeed.user_id == user.id)
    ).all().__len__() >= MAX_PERSONAL_FEEDS_PER_USER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"you already have {MAX_PERSONAL_FEEDS_PER_USER} personal feeds — "
            "disconnect one first",
        )

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

    existing = db.exec(
        select(PersonalFeed)
        .where(PersonalFeed.user_id == user.id)
        .where(PersonalFeed.feed_id == feed.id)
    ).first()
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "you already have that as a personal feed",
        )

    pf = PersonalFeed(user_id=user.id, feed_id=feed.id, label=label.strip())
    db.add(pf)
    db.commit()
    db.refresh(pf)
    return pf


@router.post("/personal-feeds", response_class=HTMLResponse)
def add_personal_feed(
    request: Request,
    url: str = Form(...),
    label: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Add a personal feed. If the pasted URL isn't a feed, run
    discovery (same as /feeds): 0 hits → 400; 1 hit → auto-add;
    N hits → picker."""
    url = normalize_feed_url((url or "").strip())
    if not url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "url required")

    # Same "don't attach to ghost rows" guard as /feeds: if the URL is
    # not in the DB OR the existing row isn't proven healthy, validate.
    from .dashboard import _feed_is_known_healthy
    existing = db.exec(select(Feed).where(Feed.url == url)).first()
    if not existing or not _feed_is_known_healthy(db, existing):
        meta = resolve_feed(url)
        if not meta.get("is_feed", True):
            candidates = discover_feeds(url)
            if not candidates:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Couldn't find an RSS feed at {url} — no "
                    "<link rel=\"alternate\"> tag and no feed at "
                    "common paths. Paste the feed URL directly "
                    "(e.g. your Mastodon .rss).",
                )
            # Always show the picker so the user confirms which feed
            # is being connected — the discovered URL differs from
            # what they typed.
            resp = _partial(
                request, "_personal_feed_discovery.html",
                candidates=candidates,
                label=label,
            )
            resp.headers["HX-Retarget"] = "#personal-discovery"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp

    pf = _register_personal_feed(db, user, url, label=label)
    feed = db.get(Feed, pf.feed_id)
    return _partial(request, "_personal_feed_row.html", pf=pf, feed=feed)


@router.post("/personal-feeds/pick", response_class=HTMLResponse)
def add_personal_feeds_selected(
    request: Request,
    urls: list[str] = Form([]),
    label: str = Form(""),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    added: list[tuple[PersonalFeed, Feed]] = []
    seen: set[str] = set()
    for raw in urls:
        u = normalize_feed_url((raw or "").strip())
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            pf = _register_personal_feed(db, user, u, label=label)
        except HTTPException as e:
            if e.status_code == status.HTTP_409_CONFLICT:
                continue
            raise
        feed = db.get(Feed, pf.feed_id)
        added.append((pf, feed))

    if not added:
        return HTMLResponse("")
    fragments = [
        templates.get_template("_personal_feed_row.html").render(
            {"pf": pf, "feed": feed, "request": request},
        )
        for pf, feed in added
    ]
    return HTMLResponse("\n".join(fragments))


@router.delete("/personal-feeds/{pf_id}")
def remove_personal_feed(
    pf_id: int,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    pf = db.get(PersonalFeed, pf_id)
    if not pf or pf.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    db.delete(pf)
    db.commit()
    # Existing Activity rows are historical and stay — followers may
    # have already seen those posts. Only the future emission stops.
    return Response(status_code=200)
