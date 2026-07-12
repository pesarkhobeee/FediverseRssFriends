"""Feed metadata resolution.

Best-effort synchronous fetch triggered when a user adds a feed URL.
Falls back to the URL's hostname if the request or parse fails; the
background fetcher (step 4) will fill in items and refresh metadata.
"""

import re
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

USER_AGENT = "rssRadar/0.1 (+https://github.com/pesarkhobeee/rssRadar)"
FETCH_TIMEOUT = 3.0


def _fallback_title(url: str) -> str:
    host = urlparse(url).netloc
    return host or url


GITHUB_URL_RE = re.compile(r"^https?://github\.com/([^/?#]+)", re.IGNORECASE)
# Path segments that are NOT a username. Everything else on github.com is
# either a user, an org, or a repo owned by one — all have a valid avatar
# at github.com/{name}.png.
_GITHUB_NON_USER = {
    "orgs", "topics", "trending", "search", "settings", "notifications",
    "explore", "marketplace", "codespaces", "features", "pricing", "about",
    "sponsors", "collections", "issues", "pulls", "new", "assets", "site",
}


def github_owner(feed_url: str) -> str | None:
    """If `feed_url` is a github.com URL, return the first path segment
    (user or org). None otherwise."""
    m = GITHUB_URL_RE.match(feed_url or "")
    if not m:
        return None
    name = m.group(1)
    # /schacon.atom → schacon; anything else with a dot isn't a username.
    if name.endswith(".atom"):
        name = name[:-5]
    if not name or "." in name or name.lower() in _GITHUB_NON_USER:
        return None
    return name


def _is_youtube_feed(feed_url: str) -> bool:
    return "youtube.com" in feed_url and "feeds/videos.xml" in feed_url


def resolve_avatar(feed_url: str, site_url: str, feed_meta=None) -> str:
    """Best guess at a display avatar for a feed. Priority:

    1. GitHub user/org avatar (real per-account photo).
    2. Feed's own <image> element (Mastodon avatars, blogs with a logo).
    3. YouTube fallback to the site favicon.
    4. Generic /favicon.ico from the site host.
    """
    owner = github_owner(feed_url)
    if owner:
        return f"https://github.com/{owner}.png?size=64"

    if feed_meta is not None:
        img = feed_image_href(feed_meta)
        if img:
            return img

    if _is_youtube_feed(feed_url):
        return "https://www.youtube.com/favicon.ico"

    return _favicon_for(site_url, feed_url)


def feed_image_href(fmeta) -> str:
    """Extract a channel-level image URL from a feedparser feed dict.
    Covers RSS <image><url/></image>, Atom <logo/> / <icon/>, and
    Mastodon-style avatar URLs. Returns '' if none present."""
    img = fmeta.get("image") if hasattr(fmeta, "get") else None
    if img:
        for key in ("href", "url", "link"):
            val = img.get(key) if isinstance(img, dict) else getattr(img, key, None)
            if val:
                return str(val).strip()
    for key in ("logo", "icon"):
        val = fmeta.get(key) if hasattr(fmeta, "get") else None
        if val:
            return str(val).strip()
    return ""


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
            "favicon_url": resolve_avatar(url, "", feed_meta=None),
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
        "favicon_url": resolve_avatar(url, site_url, feed_meta),
    }
