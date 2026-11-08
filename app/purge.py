"""Remove spam accounts and the feeds only they brought in.

    python -m app.purge --domain spam.example [--email x@y.z] [--apply]

Without --apply nothing is written; the output lists exactly what would
go. Accounts are removed with the same cascade as "Delete account".
A feed is removed only if, after that, nobody subscribes to it, it is in
no collection, it is nobody's personal feed and none of its items is
bookmarked — so a real user's feed never goes with a spammer.
"""

import argparse

from sqlmodel import Session, delete, select

from .auth import normalize_email
from .dashboard import _delete_user_cascade
from .db import (
    Bookmark,
    CollectionFeed,
    Feed,
    FeedItem,
    PersonalFeed,
    Subscription,
    User,
    engine,
)


def _matches(email: str, domains: set[str], emails: set[str]) -> bool:
    domain = email.rsplit("@", 1)[-1]
    return email in emails or any(domain == d or domain.endswith("." + d) for d in domains)


def _still_used(db: Session, feed_id: int, excluded: list[int]) -> bool:
    """True if anyone outside `excluded` relies on this feed."""
    return any(
        db.exec(stmt).first()
        for stmt in (
            select(Subscription.id)
            .where(Subscription.feed_id == feed_id, Subscription.user_id.not_in(excluded)),
            select(PersonalFeed.id)
            .where(PersonalFeed.feed_id == feed_id, PersonalFeed.user_id.not_in(excluded)),
            select(Bookmark.user_id)
            .join(FeedItem, FeedItem.id == Bookmark.feed_item_id)
            .where(FeedItem.feed_id == feed_id, Bookmark.user_id.not_in(excluded)),
            select(CollectionFeed.id).where(CollectionFeed.feed_id == feed_id),
        )
    )


def plan(db: Session, domains: set[str], emails: set[str]) -> dict:
    users = [u for u in db.exec(select(User)).all() if _matches(u.email, domains, emails)]
    ids = [u.id for u in users]
    feed_ids = sorted(
        set(db.exec(select(Subscription.feed_id).where(Subscription.user_id.in_(ids))))
        | set(db.exec(select(PersonalFeed.feed_id).where(PersonalFeed.user_id.in_(ids))))
    )
    orphan_ids = [fid for fid in feed_ids if not _still_used(db, fid, ids)]
    return {
        "users": users,
        "feeds_removed": orphan_ids,
        "feeds_kept": [fid for fid in feed_ids if fid not in orphan_ids],
        "items_removed": len(db.exec(
            select(FeedItem.id).where(FeedItem.feed_id.in_(orphan_ids))
        ).all()),
    }


def apply(db: Session, result: dict) -> None:
    ids = [u.id for u in result["users"]]
    db.exec(delete(PersonalFeed).where(PersonalFeed.user_id.in_(ids)))
    db.commit()
    for user in result["users"]:
        _delete_user_cascade(db, user)
    db.exec(delete(FeedItem).where(FeedItem.feed_id.in_(result["feeds_removed"])))
    db.exec(delete(Feed).where(Feed.id.in_(result["feeds_removed"])))
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--domain", action="append", default=[])
    parser.add_argument("--email", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    domains = {d.strip().lower().lstrip("@") for d in args.domain if d.strip()}
    emails = {normalize_email(e) for e in args.email if e.strip()}
    if not domains and not emails:
        parser.error("give at least one --domain or --email")

    with Session(engine) as db:
        result = plan(db, domains, emails)
        handles = [u.handle for u in result["users"]]
        emails_found = [u.email for u in result["users"]]
        if args.apply:
            apply(db, result)
    print(f"{'DELETED' if args.apply else 'DRY RUN — would delete'}:")
    print(f"  {len(handles)} accounts: {' '.join(handles)}")
    for email in emails_found:
        print(f"    {email}")
    print(f"  {len(result['feeds_removed'])} feeds: {result['feeds_removed']}")
    print(f"  {result['items_removed']} items")
    if result["feeds_kept"]:
        print(f"  kept (still used by others): feeds {result['feeds_kept']}")
    if not args.apply:
        print("Nothing was changed. Re-run with --apply to delete.")


if __name__ == "__main__":
    main()
