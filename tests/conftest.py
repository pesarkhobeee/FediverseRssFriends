"""Test setup — set env before importing the app, reset schema per test."""

import os
import tempfile

# These must be set BEFORE `app.*` is imported, because config captures env at import time.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["RSSRADAR_DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["RSSRADAR_SESSION_SECRET"] = "test-secret"
os.environ["RSSRADAR_BASE_URL"] = "http://testserver"
os.environ.setdefault("RSSRADAR_MAGIC_LINK_MIN_INTERVAL", "0")
os.environ["RSSRADAR_ENABLE_SCHEDULER"] = "0"

import pytest
from sqlmodel import SQLModel

import app.db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_schema():
    SQLModel.metadata.drop_all(app.db.engine)
    app.db.init_db()
    yield
