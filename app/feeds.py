"""Feed metadata resolution.

Best-effort synchronous fetch triggered when a user adds a feed URL.
Falls back to the URL's hostname if the request or parse fails; the
background fetcher (step 4) will fill in items and refresh metadata.
"""

import re
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

USER_AGENT = "rssFriends/0.1 (+https://github.com/pesarkhobeee/rssFriends)"
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


_REDDIT_SUB_RE = re.compile(
    r"^(https?://(?:www\.|old\.)?reddit\.com/r/[^/?#]+)/?$", re.IGNORECASE
)
_GITHUB_PROFILE_RE = re.compile(r"^https?://github\.com/[^/?#]+/?$", re.IGNORECASE)


def normalize_feed_url(url: str) -> str:
    """Rewrite well-known 'feed-like but not actually a feed' URLs to their
    real endpoint. Two rules — the sources that most often appear as plain
    profile URLs in exported OPML files:

      github.com/schacon        → github.com/schacon.atom
      reddit.com/r/rss/         → reddit.com/r/rss/.rss

    URLs that already look like real feeds (github.com/user.atom,
    github.com/org/repo/releases.atom, reddit.com/r/x/.rss, .../top.rss)
    are returned untouched.
    """
    url = (url or "").strip()
    if not url:
        return url

    # Bare GitHub profile → .atom. github_owner() rejects non-user paths
    # (orgs/, topics/, marketplace, …) and anything already containing a
    # dot in the first segment.
    if _GITHUB_PROFILE_RE.match(url) and github_owner(url):
        return f"https://github.com/{github_owner(url)}.atom"

    m = _REDDIT_SUB_RE.match(url)
    if m:
        return f"{m.group(1)}/.rss"

    return url


# Extracts href from <link rel="…icon…"> in HTML. Rel can come before or
# after href, and quotes vary. Two patterns cover the common shapes.
_LINK_ICON_REL_FIRST = re.compile(
    r'<link[^>]*\brel=["\']?[^"\'>]*icon[^"\'>]*["\']?[^>]*\bhref=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_LINK_ICON_HREF_FIRST = re.compile(
    r'<link[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']?[^"\'>]*icon',
    re.IGNORECASE,
)
_OG_IMAGE = re.compile(
    r'<meta[^>]*\bproperty=["\']og:image["\'][^>]*\bcontent=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _http_get_text(url: str, timeout: float = 3.0) -> str | None:
    """Best-effort HTML fetch. Returns None on any failure."""
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text
    except Exception:
        return None


def _youtube_channel_avatar(feed_url: str) -> str | None:
    """Scrape the channel's YouTube page for the og:image (the avatar).
    Only useful in a deep resolve — costs one HTTP round-trip."""
    m = re.search(r"channel_id=([A-Za-z0-9_\-]+)", feed_url)
    if not m:
        return None
    html = _http_get_text(f"https://www.youtube.com/channel/{m.group(1)}")
    if not html:
        return None
    hit = _OG_IMAGE.search(html)
    return hit.group(1).strip() if hit else None


def _site_icon_from_html(site_url: str) -> str | None:
    """Fetch the site's homepage and return the first <link rel=…icon…>
    href, resolved to an absolute URL. Costs one HTTP round-trip."""
    if not site_url:
        return None
    html = _http_get_text(site_url)
    if not html:
        return None
    hit = _LINK_ICON_REL_FIRST.search(html) or _LINK_ICON_HREF_FIRST.search(html)
    if not hit:
        return None
    href = hit.group(1).strip()
    if not href:
        return None
    # Protocol-relative URLs (//cdn.example.com/…) — attach https so we
    # store a self-contained absolute URL, not something whose meaning
    # depends on where it's rendered.
    if href.startswith("//"):
        return "https:" + href
    return href if urlparse(href).netloc else urljoin(site_url, href)


def resolve_avatar(
    feed_url: str, site_url: str, feed_meta=None, *, deep: bool = False
) -> str:
    """Best guess at a display avatar for a feed.

    Fast (deep=False, used in every refresh cycle):
      GitHub owner → feed's <image> → YouTube-site favicon → /favicon.ico

    Deep (deep=True, one-time on subscribe + manual per-feed refresh):
      + YouTube channel avatar (og:image scrape)
      + <link rel="icon"> from the site's HTML if we'd otherwise
        return the generic /favicon.ico
    """
    owner = github_owner(feed_url)
    if owner:
        return f"https://github.com/{owner}.png?size=64"

    # YouTube channel avatar — only paid for on deep resolves.
    if _is_youtube_feed(feed_url) and deep:
        yt_avatar = _youtube_channel_avatar(feed_url)
        if yt_avatar:
            return yt_avatar
        # If we couldn't scrape, fall through to feed image / site favicon.

    if feed_meta is not None:
        img = feed_image_href(feed_meta)
        if img:
            return img

    # Generic sites: if this is a deep pass, try to discover the real
    # icon URL from the HTML before falling back to /favicon.ico.
    if deep and site_url:
        found = _site_icon_from_html(site_url)
        if found:
            return found

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
    url = normalize_feed_url(url.strip())
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
    # GitHub user/org feeds all ship the generic
    # "Github Public timeline feed" (or similar) title. Prefer the
    # username / org name instead — matches lovelyRSS.
    owner = github_owner(url)
    if owner:
        title = owner
    site_url = (feed_meta.get("link") or "").strip()
    # Some feeds give a relative site link — resolve against the feed URL.
    if site_url and not urlparse(site_url).netloc:
        site_url = urljoin(url, site_url)

    return {
        "title": title,
        "site_url": site_url,
        "favicon_url": resolve_avatar(url, site_url, feed_meta, deep=True),
    }
