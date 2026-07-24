"""YouTube URL discovery (0.8.6).

Two failure modes that used to combine into "Couldn't find an RSS feed
at that URL" for legitimate YouTube channels:

1. Legacy `/user/X` and `/channel/X` URL shapes never went through
   normalize_feed_url — discovery had to HTML-scrape the channel page
   to find the RSS <link>, but our EU-hosted container hit YouTube's
   GDPR consent wall and got a cookie page with no RSS tag.

2. Newer `/@handle` and `/c/name` shapes have no deterministic feed
   URL so they DO need HTML scraping. Same consent-wall problem.

Fix: normalize the two deterministic shapes to their feeds.xml URLs
directly, and send Google's CONSENT + SOCS cookies (scoped to
.youtube.com so they cross the consent.youtube.com redirect) on
every httpx request so the wall is skipped for the scrape-required
shapes too.

These tests hit YouTube's real endpoints — they're network-dependent
and would flake in a strict offline CI. Guarded with `pytest.mark.
network` so they can be skipped locally with `-m "not network"`."""

import pytest

from app.feeds import normalize_feed_url

# ---- Unit: normalize_feed_url rules (no network) -----------------------


def test_youtube_user_url_normalizes_to_feed():
    assert (
        normalize_feed_url("https://www.youtube.com/user/WorldVisionUSA")
        == "https://www.youtube.com/feeds/videos.xml?user=WorldVisionUSA"
    )


def test_youtube_channel_url_normalizes_to_feed():
    assert (
        normalize_feed_url(
            "https://www.youtube.com/channel/UCtxKQOd8elJbs7wn9b7gevg"
        )
        == "https://www.youtube.com/feeds/videos.xml"
        "?channel_id=UCtxKQOd8elJbs7wn9b7gevg"
    )


def test_youtube_user_url_lowercase_scheme_and_no_www():
    assert (
        normalize_feed_url("http://youtube.com/user/Example")
        == "https://www.youtube.com/feeds/videos.xml?user=Example"
    )


def test_youtube_channel_id_must_look_like_a_channel_id():
    """A path segment that isn't a UC.. channel ID must NOT be
    rewritten as a feed URL — otherwise `/channel/foo` would silently
    become `/feeds/videos.xml?channel_id=foo` and produce a broken
    Feed row. Real YouTube channel IDs are always `UC` + 22 chars."""
    assert (
        normalize_feed_url("https://www.youtube.com/channel/notarealid")
        == "https://www.youtube.com/channel/notarealid"
    )


def test_youtube_handle_url_is_left_alone_for_html_scraping():
    """@handle and /c/name don't have a deterministic feed URL — they
    fall through to discover_feeds() which HTML-scrapes the channel
    page. normalize_feed_url must not touch them."""
    for u in (
        "https://www.youtube.com/@LinusTechTips",
        "https://www.youtube.com/c/veritasium",
    ):
        assert normalize_feed_url(u) == u


def test_consent_cookies_scope_to_youtube_domain():
    """The consent cookie jar is set with `domain='.youtube.com'`
    so cookies travel across the `youtube.com → consent.youtube.com`
    redirect. A plain dict would scope to only the initial host and
    the fix wouldn't work."""
    from app.feeds import _CONSENT_COOKIES

    domains = {c.domain for c in _CONSENT_COOKIES.jar}
    assert ".youtube.com" in domains


# ---- Integration: real YouTube fetch (network) -------------------------


@pytest.mark.network
def test_youtube_channel_url_resolves_to_real_feed():
    """End-to-end: /channel/{id} normalizes then successfully resolves
    as a real feed. Skipped in strict offline runs."""
    from app.feeds import resolve_feed
    m = resolve_feed(
        "https://www.youtube.com/channel/UCtxKQOd8elJbs7wn9b7gevg"
    )
    assert m["is_feed"] is True
    assert m["title"]


@pytest.mark.network
def test_youtube_at_handle_url_discovers_feed_via_consent_bypass():
    """The @handle shape can't be normalized — depends on the CONSENT
    cookie in _http_get_text to skip the consent wall so the RSS link
    is visible in the channel HTML."""
    from app.feeds import discover_feeds
    candidates = discover_feeds("https://www.youtube.com/@LinusTechTips")
    assert candidates, "expected at least one feed candidate"
    assert any(
        "feeds/videos.xml" in c["url"] for c in candidates
    ), f"no /feeds/videos.xml in candidates: {candidates}"
