"""Deterministic colored-initial avatars for user radars.

Every user gets a circular avatar rendered as an accent-colored disc
with 1–2 initials. No image upload, no external service, no DB column.
Everything derives from `User.handle` via a stable hash so the avatar
looks the same everywhere (radar header, follower lists, timeline,
etc.) and never changes unless the handle does.

Palette is curated (not random hue rotation) so colors stay in the
same warm-editorial family as the rest of the design language.
"""

from __future__ import annotations

import hashlib

# Curated palette. All colors carry white text at ≥ 4.5:1 contrast so
# they're readable both light and dark modes. Keep the list short —
# there's real value in a friend's color being recognizable across the
# app, and randomness dilutes that.
_PALETTE: tuple[str, ...] = (
    "#264653",  # deep teal
    "#2A9D8F",  # muted teal-green
    "#E76F51",  # coral
    "#E9C46A",  # sand (uses dark text — handled below)
    "#8A5A83",  # mauve
    "#4A6FA5",  # cornflower
    "#997B2F",  # olive-gold
    "#5F7A4E",  # sage
    "#C75B1E",  # accent burnt-orange
    "#3D5A80",  # slate blue
    "#B5525C",  # dusty rose
    "#6B7280",  # neutral gray
)

# Colors that need dark text to hit contrast. Keep in sync with _PALETTE.
_LIGHT_BGS: frozenset[str] = frozenset({"#E9C46A"})


def _hash(handle: str) -> int:
    """Stable positive integer from a handle. SHA-1 is overkill for a
    palette index but it's stdlib, deterministic across restarts, and
    avoids Python's PYTHONHASHSEED randomization."""
    return int(hashlib.sha1(handle.encode("utf-8")).hexdigest()[:8], 16)


def initials(handle: str) -> str:
    """Return 1–2 uppercase letters representing the handle.

    Splits on separators (`-`, `_`, `.`) and takes the first letter of
    the first two words. Falls back to the first two letters of the
    whole handle if there are no separators. Empty input returns '??'."""
    if not handle:
        return "??"
    parts = [p for p in handle.replace("_", "-").replace(".", "-").split("-") if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    only = parts[0] if parts else handle
    return only[:2].upper()


def color(handle: str) -> str:
    """Return a stable hex background color for `handle`."""
    if not handle:
        return _PALETTE[0]
    return _PALETTE[_hash(handle) % len(_PALETTE)]


def text_color(bg_hex: str) -> str:
    """Given an avatar background, return the readable text color."""
    return "#221B12" if bg_hex in _LIGHT_BGS else "#FBF8F3"
