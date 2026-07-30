"""Test setup — set env before importing the app, reset schema per test."""

import os
import tempfile

# These must be set BEFORE `app.*` is imported, because config captures env at import time.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["RSSFRIENDS_DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["RSSFRIENDS_SESSION_SECRET"] = "test-secret"
os.environ["RSSFRIENDS_BASE_URL"] = "http://testserver"
os.environ.setdefault("RSSFRIENDS_MAGIC_LINK_MIN_INTERVAL", "0")
os.environ["RSSFRIENDS_ENABLE_SCHEDULER"] = "0"

import pytest
from sqlmodel import SQLModel

import app.db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_schema():
    SQLModel.metadata.drop_all(app.db.engine)
    app.db.init_db()
    yield


@pytest.fixture(autouse=True)
def _no_inline_fetch(monkeypatch):
    """0.9.4: POST /feeds now runs a synchronous fetch_one after a
    new subscribe so the user sees immediate item-count confirmation.
    Bypass that in tests — otherwise every subscribe would hit the
    real feed URL over the network (slow, flaky, and clobbers the
    resolve_feed stubs several tests depend on for stable titles)."""
    import app.dashboard
    monkeypatch.setattr(
        app.dashboard, "_fetch_inline_for_new_subscription",
        lambda db, feed: 0,
    )
