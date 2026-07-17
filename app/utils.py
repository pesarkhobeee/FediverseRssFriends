"""Small formatting helpers shared across templates."""

import re
from datetime import UTC, datetime, timedelta

import nh3

# Allow a conservative set of tags: enough for readable prose, nothing
# that can execute JS, load remote assets, or break layout. `img` is
# intentionally excluded — tracking pixels + layout jumps aren't worth
# it on a preview page; users click through for the full post.
# Headings (h1–h6) are excluded too — the item title already IS the
# heading, and feeds that embed a big <h1> from the source article
# were breaking the visual rhythm.
_ALLOWED_TAGS: set[str] = {
    "p", "br", "hr",
    "strong", "em", "b", "i", "u", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a",
}
_ALLOWED_ATTRS: dict[str, set[str]] = {"a": {"href", "title"}}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
EXCERPT_CHARS = 280


def relative_time(dt: datetime | None) -> str:
    """'2 hours ago' / 'yesterday' / etc. Empty string if dt is None."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    if delta < timedelta(seconds=0):
        return "just now"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def source_domain(url: str | None) -> str:
    """Return the host portion of a URL, stripping leading 'www.'. Used
    as a source attribution chip on cross-posted activity items."""
    if not url:
        return ""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def clean_summary(html: str | None) -> str:
    """Sanitize feed-provided HTML for safe rendering.

    Feeds are untrusted input — an entry.summary can contain <script>,
    inline event handlers, remote iframes, or worse. `nh3` strips
    everything not in the allow-list and forces external links to open
    with rel=noopener via its default link-rel handling."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        link_rel="noopener noreferrer",
    )


def summary_excerpt(html: str | None, max_chars: int = EXCERPT_CHARS) -> str:
    """Plain-text excerpt suitable for the collapsed 'preview' line.

    Strips ALL HTML (not just the disallowed subset — even <p>) and
    collapses whitespace, then truncates at a word boundary and appends
    an ellipsis if we cut anything off."""
    if not html:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",.;:—- ") + "…"


def summary_was_truncated(html: str | None, max_chars: int = EXCERPT_CHARS) -> bool:
    """True if summary_excerpt() would have cut off content — used by
    the template to decide whether to render a 'Read more' toggle."""
    if not html:
        return False
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()
    return len(text) > max_chars
