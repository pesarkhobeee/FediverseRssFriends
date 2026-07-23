"""Colored-initial avatar helpers (app/avatars.py).

The output must be *stable* across restarts (no PYTHONHASHSEED
randomness) — the same handle produces the same color forever."""

import app.db as db_mod
import app.main
from app.avatars import _PALETTE, color, initials, text_color


def test_initials_two_word_handle():
    assert initials("ahmadian-farid") == "AF"


def test_initials_underscore_and_dot_separators_normalize():
    assert initials("john_doe") == "JD"
    assert initials("first.last") == "FL"


def test_initials_single_word_takes_first_two_letters():
    assert initials("mo") == "MO"
    assert initials("alice") == "AL"


def test_initials_empty_returns_double_qmark():
    assert initials("") == "??"


def test_color_is_stable_across_calls():
    """Same handle must always map to the same palette entry —
    otherwise a user's identity color would rotate between requests."""
    a = color("ahmadian-farid")
    b = color("ahmadian-farid")
    assert a == b
    assert a in _PALETTE


def test_color_differs_for_different_handles():
    """With 12 palette entries and normal handle diversity, at least
    one of a couple distinct handles should land on a different color."""
    seen = {color(h) for h in ("alice", "bob", "carol", "dave", "eve",
                               "frank", "grace", "heidi")}
    assert len(seen) >= 3


def test_text_color_flips_for_light_backgrounds():
    """The one light-yellow entry needs dark text; every other palette
    color needs cream text. This is the readability contract."""
    for bg in _PALETTE:
        fg = text_color(bg)
        assert fg in ("#221B12", "#FBF8F3")
    assert text_color("#E9C46A") == "#221B12"  # the light-bg exception


def test_radar_page_renders_avatar_markup():
    """End-to-end: /u/{handle} produces a .user-avatar element with the
    correct background color + initials for the owner."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    with Session(db_mod.engine) as s:
        u = db_mod.User(email="john@e.com", handle="john-doe")
        s.add(u)
        s.commit()

    r = TestClient(app.main.app).get("/u/john-doe")
    assert r.status_code == 200
    assert 'class="user-avatar"' in r.text
    assert ">JD<" in r.text  # initials rendered
    assert color("john-doe") in r.text  # deterministic bg color
