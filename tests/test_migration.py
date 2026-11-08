"""Startup migration: adds activity_secret to an existing DB missing it."""

import sqlite3

from sqlmodel import Session, select


def test_migration_adds_activity_secret_column_and_backfills():
    """Simulate an old DB predating step 9: drop the column, insert a user
    with legacy schema, run init_db(), and confirm the column is present
    and backfilled with a non-empty secret."""
    import app.db as db_mod

    # Drop and recreate the user table WITHOUT activity_secret, then insert
    # a row directly via sqlite3 to bypass SQLModel's schema requirements.
    engine_path = db_mod.engine.url.database
    conn = sqlite3.connect(engine_path)
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS user;
            CREATE TABLE user (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                handle TEXT UNIQUE NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'public',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO user (email, handle) VALUES ('old@example.com', 'olduser');
            """
        )
        conn.commit()
    finally:
        conn.close()

    db_mod.init_db()

    with Session(db_mod.engine) as s:
        user = s.exec(select(db_mod.User).where(db_mod.User.handle == "olduser")).one()
    assert user.activity_secret
    assert user.login_count == 0
    assert user.last_login_at is None
    assert len(user.activity_secret) >= 16
