"""Feed metadata resolution.

Best-effort synchronous fetch triggered when a user adds a feed URL.
Falls back to the URL's hostname if the request or parse fails; the
background fetcher (step 4) will fill in items and refresh metadata.
"""

from urllib.parse import urljoin, urlparse

import feedparser
import httpx

USER_AGENT = "rssRadar/0.1 (+https://github.com/pesarkhobeee/rssRadar)"
FETCH_TIMEOUT = 3.0


def _fallback_title(url: str) -> str:
    host = urlparse(url).netloc
    return host or url


def _favicon_for(site_url: str, feed_url: str) -> str:
    origin = urlparse(site_url or feed_url)
    if not origin.netloc:
        return ""
    return f"{origin.scheme or 'https'}://{origin.netloc}/favicon.ico"


def resolve_feed(url: str) -> dict:
    """Return {title, site_url, favicon_url} for a feed URL. Never raises."""
    url = url.strip()
    if not url:
        return {"title": "", "site_url": "", "favicon_url": ""}

    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
    except Exception:
        return {
            "title": _fallback_title(url),
            "site_url": "",
            "favicon_url": _favicon_for("", url),
        }

    feed_meta = getattr(parsed, "feed", {}) or {}
    title = (feed_meta.get("title") or "").strip() or _fallback_title(url)
    site_url = (feed_meta.get("link") or "").strip()
    # Some feeds give a relative site link — resolve against the feed URL.
    if site_url and not urlparse(site_url).netloc:
        site_url = urljoin(url, site_url)

    return {
        "title": title,
        "site_url": site_url,
        "favicon_url": _favicon_for(site_url, url),
    }
