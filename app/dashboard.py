"""Dashboard CRUD: categories + feed subscriptions.

All state-changing routes return small HTML fragments so HTMX can swap
them into the page. Full-page renders live at GET /dashboard.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import update
from sqlmodel import Session, select

from . import activity
from .auth import login_required
from .db import (
    Activity,
    Category,
    Favorite,
    Feed,
    FeedItem,
    Follow,
    MagicToken,
    Subscription,
    User,
    get_session,
)
from .feeds import normalize_feed_url, resolve_avatar, resolve_feed
from .importers import add_subscription_by_url
from .opml import parse_opml
from .refresh import fetch_one, kick_background_refresh, run_cycle
from .templating import make_templates

templates = make_templates()

router = APIRouter()


def _partial(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _load_radar(db: Session, user: User) -> list[dict]:
    """Return categories in order, each with its subscriptions + feed metadata + item count."""
    from sqlalchemy import func

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

    # Cache item counts per feed in one query.
    feed_ids = {feed.id for _sub, feed in subs}
    counts: dict[int, int] = {}
    if feed_ids:
        rows = db.exec(
            select(FeedItem.feed_id, func.count(FeedItem.id))
            .where(FeedItem.feed_id.in_(feed_ids))
            .group_by(FeedItem.feed_id)
        ).all()
        counts = {fid: n for fid, n in rows}

    by_cat: dict[int, list[dict]] = {c.id: [] for c in categories}
    for sub, feed in subs:
        by_cat.setdefault(sub.category_id, []).append(
            {"sub": sub, "feed": feed, "item_count": counts.get(feed.id, 0)}
        )

    return [{"category": c, "subs": by_cat.get(c.id, [])} for c in categories]


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    from .follow import pending_requests_count
    return _partial(
        request,
        "dashboard.html",
        user=user,
        radar=_load_radar(db, user),
        pending_requests=pending_requests_count(db, user.id),
    )


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
    db.flush()
    activity.record(db, user, "created_category", cat.name, ref_type="category", ref_id=cat.id)
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
    url = normalize_feed_url(url.strip())
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
    activity.record(
        db, user, "subscribed", feed.title,
        subject_url=feed.site_url or feed.url,
        ref_type="feed", ref_id=feed.id,
    )
    db.commit()
    db.refresh(sub)
    return _partial(request, "_feed_row.html", sub=sub, feed=feed, item_count=0)


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


def _get_owned_subscription(db: Session, user: User, sub_id: int) -> Subscription:
    sub = db.get(Subscription, sub_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return sub


@router.get("/subscriptions/{sub_id}/move-picker", response_class=HTMLResponse)
def move_picker(
    sub_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    sub = _get_owned_subscription(db, user, sub_id)
    categories = db.exec(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.sort_order, Category.id)
    ).all()
    return _partial(request, "_move_picker.html", sub=sub, categories=categories)


@router.get("/subscriptions/{sub_id}/actions", response_class=HTMLResponse)
def subscription_actions(
    sub_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Return the default row actions — used by 'Cancel' on the move picker
    to restore the button strip without a full page reload."""
    sub = _get_owned_subscription(db, user, sub_id)
    feed = db.get(Feed, sub.feed_id)
    return _partial(request, "_feed_actions.html", sub=sub, feed=feed)


@router.post("/subscriptions/{sub_id}/move")
def move_subscription(
    sub_id: int,
    category_id: int = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Move a subscription to a different category on the same radar.
    Both the sub and the target category must belong to the caller."""
    sub = _get_owned_subscription(db, user, sub_id)
    target = db.get(Category, category_id)
    if not target or target.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your category")

    if sub.category_id != target.id:
        # Place the moved feed at the end of the destination's list.
        from sqlalchemy import func
        max_order = db.exec(
            select(func.max(Subscription.sort_order))
            .where(Subscription.user_id == user.id)
            .where(Subscription.category_id == target.id)
        ).one() or 0
        sub.category_id = target.id
        sub.sort_order = max_order + 1
        db.add(sub)
        db.commit()

    # Simplest robust UX for a rare action: tell HTMX to re-fetch the
    # dashboard so both categories re-render with the moved row in its
    # new home. No OOB coordination needed.
    return Response(status_code=204, headers={"HX-Refresh": "true"})


MAX_OPML_BYTES = 2 * 1024 * 1024  # 2 MB is huge for OPML


@router.post("/import/opml", response_class=HTMLResponse)
async def import_opml(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    content = await file.read(MAX_OPML_BYTES + 1)
    if len(content) > MAX_OPML_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "OPML too large")
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    try:
        entries = parse_opml(content)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    added = 0
    dup = 0
    for e in entries:
        was_added, _feed = add_subscription_by_url(
            db, user, e.xml_url, e.category,
            title_hint=e.title or None,
            site_hint=e.html_url or None,
        )
        if was_added:
            added += 1
        else:
            dup += 1
    db.commit()

    if added:
        kick_background_refresh()

    return _partial(
        request,
        "_import_result.html",
        result={"added": added, "duplicates": dup, "total": len(entries)},
    )


@router.post("/feeds/{feed_id}/refresh", response_class=HTMLResponse)
def refresh_single_feed(
    feed_id: int,
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Force a synchronous refresh of a single feed. The caller must be
    subscribed — feeds are global, so any subscriber can trigger a refetch."""
    sub = db.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.feed_id == feed_id)
    ).first()
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not subscribed")
    feed = db.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "feed not found")

    new, err = fetch_one(db, feed)
    # Manual refresh is a good moment to upgrade the avatar too — the
    # scheduled cycle only does the fast path, so YouTube channels stay
    # generic without this.
    deep_avatar = resolve_avatar(feed.url, feed.site_url, None, deep=True)
    if deep_avatar and deep_avatar != feed.favicon_url:
        feed.favicon_url = deep_avatar
        db.add(feed)
        db.commit()
    return _partial(
        request,
        "_feed_refreshed.html",
        new=new,
        err=err,
        feed=feed,
    )


# ---------- Account deletion ----------


def _account_footprint(db: Session, user: User) -> dict[str, int]:
    """Counts we show the user on the confirmation screen so they know
    exactly what's about to be wiped. Cheap aggregate queries — one
    row per counter."""
    def n(stmt) -> int:
        return db.exec(stmt).one() or 0

    from sqlalchemy import func
    return {
        "subscriptions": n(select(func.count(Subscription.id))
                           .where(Subscription.user_id == user.id)),
        "categories": n(select(func.count(Category.id))
                        .where(Category.user_id == user.id)),
        "favorites": n(select(func.count()).select_from(Favorite)
                       .where(Favorite.user_id == user.id)),
        "following": n(select(func.count()).select_from(Follow)
                       .where(Follow.follower_id == user.id)),
        "followers": n(select(func.count()).select_from(Follow)
                       .where(Follow.followee_id == user.id)),
        "activity": n(select(func.count(Activity.id))
                      .where(Activity.user_id == user.id)),
    }


def _delete_user_cascade(db: Session, user: User) -> None:
    """Wipe every row this user owns, and null out foreign references
    from third-party rows. Feeds/FeedItems are shared and untouched —
    a feed with zero subscribers just stops being refreshed elsewhere.

    Third-party Activity rows that named this user as `ref_id` (e.g.
    "@bob forked @alice") keep their denormalized subject text and get
    `ref_id` set to NULL — so timelines don't dangle-point at a
    nonexistent row but history remains readable."""
    uid = user.id
    email = user.email
    for stmt in (
        Favorite.__table__.delete().where(Favorite.user_id == uid),
        Subscription.__table__.delete().where(Subscription.user_id == uid),
        Category.__table__.delete().where(Category.user_id == uid),
        Follow.__table__.delete().where(
            (Follow.follower_id == uid) | (Follow.followee_id == uid)
        ),
        Activity.__table__.delete().where(Activity.user_id == uid),
        update(Activity).where(
            Activity.ref_type == "user", Activity.ref_id == uid
        ).values(ref_id=None),
        MagicToken.__table__.delete().where(MagicToken.email == email),
        User.__table__.delete().where(User.id == uid),
    ):
        db.exec(stmt)
    db.commit()


@router.get("/account/delete-confirm", response_class=HTMLResponse)
def delete_confirm(
    request: Request,
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    return _partial(
        request,
        "_delete_account.html",
        user=user,
        footprint=_account_footprint(db, user),
    )


@router.post("/account/delete")
def delete_account(
    request: Request,
    confirm_handle: str = Form(...),
    user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Destructive. Requires the user to re-type their handle as a
    second affirmation — the client-side JS already enforces this,
    but we re-check server-side so a bypassed form can't sneak
    through."""
    if confirm_handle.strip().lstrip("@").lower() != user.handle.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "handle does not match — account not deleted",
        )
    _delete_user_cascade(db, user)
    request.session.clear()
    return RedirectResponse("/?deleted=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/refresh", response_class=HTMLResponse)
def manual_refresh(
    request: Request,
    _user: User = Depends(login_required),
    db: Session = Depends(get_session),
):
    """Kick off one refresh cycle synchronously. Returns the fresh dashboard
    body so HTMX can swap it in place."""
    result = run_cycle()
    return _partial(
        request,
        "_refresh_flash.html",
        result=result,
    )
