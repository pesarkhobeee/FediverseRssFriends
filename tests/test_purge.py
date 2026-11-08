"""python -m app.purge: spam account + orphan feed removal."""

from sqlmodel import Session, select

from app.db import (
    Bookmark,
    Category,
    Collection,
    CollectionFeed,
    Feed,
    FeedItem,
    Subscription,
    User,
    engine,
)
from app.purge import apply, plan


def _user(db, email: str) -> User:
    user = User(email=email, handle=email.split("@")[0])
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _subscribe(db, user: User, url: str) -> Feed:
    feed = db.exec(select(Feed).where(Feed.url == url)).first()
    if feed is None:
        feed = Feed(url=url, title=url)
        db.add(feed)
        db.commit()
        db.refresh(feed)
        db.add(FeedItem(feed_id=feed.id, guid=f"{url}#1", title="item"))
    cat = Category(user_id=user.id, name="Guides")
    db.add(cat)
    db.commit()
    db.add(Subscription(user_id=user.id, feed_id=feed.id, category_id=cat.id))
    db.commit()
    return feed


def _seed():
    with Session(engine) as db:
        spam = _user(db, "bot@spam.example")
        sub_spam = _user(db, "bot2@mx.spam.example")
        alias = _user(db, "x+spam@gmail.example")
        real = _user(db, "real@good.example")

        only_spam = _subscribe(db, spam, "https://catbox.example/a.xml")
        _subscribe(db, sub_spam, "https://catbox.example/b.xml")
        _subscribe(db, alias, "https://catbox.example/c.xml")
        shared = _subscribe(db, spam, "https://blog.example/feed.xml")
        _subscribe(db, real, "https://blog.example/feed.xml")

        in_collection = _subscribe(db, spam, "https://curated.example/feed.xml")
        col = Collection(slug="c", name="C")
        db.add(col)
        db.commit()
        db.add(CollectionFeed(collection_id=col.id, feed_id=in_collection.id))

        bookmarked = _subscribe(db, spam, "https://saved.example/feed.xml")
        item = db.exec(select(FeedItem).where(FeedItem.feed_id == bookmarked.id)).one()
        db.add(Bookmark(user_id=real.id, feed_item_id=item.id))
        db.commit()
        return {
            "only_spam": only_spam.id, "shared": shared.id,
            "in_collection": in_collection.id, "bookmarked": bookmarked.id,
        }


def _counts():
    with Session(engine) as db:
        return (
            len(db.exec(select(User)).all()),
            len(db.exec(select(Feed)).all()),
            len(db.exec(select(FeedItem)).all()),
        )


def test_plan_writes_nothing():
    ids = _seed()
    before = _counts()
    with Session(engine) as db:
        result = plan(db, {"spam.example"}, {"x+spam@gmail.example"})
    assert sorted(u.handle for u in result["users"]) == ["bot", "bot2", "x+spam"]
    assert ids["only_spam"] in result["feeds_removed"]
    assert len(result["feeds_removed"]) == 3
    assert _counts() == before


def test_apply_removes_spam_and_keeps_anything_real_users_need():
    ids = _seed()
    with Session(engine) as db:
        apply(db, plan(db, {"spam.example"}, {"x+spam@gmail.example"}))
    with Session(engine) as db:
        assert [u.handle for u in db.exec(select(User)).all()] == ["real"]
        remaining = {f.id for f in db.exec(select(Feed)).all()}
        assert ids["only_spam"] not in remaining
        assert {ids["shared"], ids["in_collection"], ids["bookmarked"]} <= remaining
        assert len(remaining) == 3
        assert not db.exec(
            select(FeedItem).where(FeedItem.feed_id == ids["only_spam"])
        ).first()
        assert db.exec(select(Bookmark)).first() is not None


def test_lookalike_domain_untouched():
    _seed()
    with Session(engine) as db:
        assert plan(db, {"am.example"}, set())["users"] == []
