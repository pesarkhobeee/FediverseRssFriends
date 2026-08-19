"""Feed metadata resolution.

Best-effort synchronous fetch triggered when a user adds a feed URL.
Falls back to the URL's hostname if the request or parse fails; the
background fetcher (step 4) will fill in items and refresh metadata.
"""

import re
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

USER_AGENT = "FediverseRssFriends/0.1 (+https://github.com/pesarkhobeee/FediverseRssFriends)"
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

# YouTube shapes that have a deterministic feed URL — no HTML scraping
# needed. See normalize_feed_url() for context.
_YT_USER_RE = re.compile(
    r"^https?://(?:www\.)?youtube\.com/user/([A-Za-z0-9_-]+)/?$", re.IGNORECASE
)
_YT_CHANNEL_RE = re.compile(
    r"^https?://(?:www\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{22})/?$",
    re.IGNORECASE,
)


def normalize_feed_url(url: str) -> str:
    """Rewrite well-known 'feed-like but not actually a feed' URLs to their
    real endpoint. Rules cover the sources that most often appear as plain
    profile URLs in exported OPML files or pasted by hand:

      github.com/schacon                → github.com/schacon.atom
      reddit.com/r/rss/                 → reddit.com/r/rss/.rss
      youtube.com/user/{X}              → youtube.com/feeds/videos.xml?user={X}
      youtube.com/channel/{UC...}       → youtube.com/feeds/videos.xml?channel_id={UC...}

    URLs that already look like real feeds (github.com/user.atom,
    github.com/org/repo/releases.atom, reddit.com/r/x/.rss, .../top.rss,
    youtube.com/feeds/videos.xml?...) are returned untouched.

    YouTube shapes are handled here specifically because our egress IP
    (EU-hosted) triggers Google's GDPR consent-wall on the /user/ and
    /channel/ paths — so HTML-scraping discovery of <link rel="alternate">
    can never see the RSS tag from us. The /feeds/videos.xml endpoints
    are unauthenticated and consent-free.
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

    m = _YT_USER_RE.match(url)
    if m:
        return f"https://www.youtube.com/feeds/videos.xml?user={m.group(1)}"

    m = _YT_CHANNEL_RE.match(url)
    if m:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"

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
_META_REFRESH_RE = re.compile(
    r'<meta\b[^>]*\bhttp-equiv=["\']?refresh["\']?[^>]*\bcontent=["\'][^"\']*?url=([^"\'>]+)',
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
            cookies=_CONSENT_COOKIES,
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text
    except Exception:
        return None


# Google/YouTube's GDPR consent wall serves EU-IP visitors an
# interstitial page instead of the requested URL — that page has no
# <link rel="alternate"> RSS tag, so HTML-scraping discovery on
# youtube.com/@handle etc. always drew a blank from our EU-hosted
# container. Presenting these cookies preemptively tells Google we've
# already consented, so channel pages render as normal on the first
# request (no consent-page bounce).
#
# CONSENT=YES is the canonical "user chose accept" sentinel; SOCS=CAI
# is the newer Google Session Overlay Consent Signal that some
# YouTube endpoints check in addition. Both scoped to `.youtube.com`
# so the cookies are sent to BOTH `www.youtube.com` and
# `consent.youtube.com` — a plain `cookies={...}` dict would scope to
# only the initial host and drop the cookie on the cross-subdomain
# redirect, sending us back to the wall.
def _build_consent_cookies() -> "httpx.Cookies":
    jar = httpx.Cookies()
    jar.set("CONSENT", "YES+cb.20210328-17-p0.en+FX+842",
            domain=".youtube.com", path="/")
    jar.set("SOCS", "CAI", domain=".youtube.com", path="/")
    return jar


_CONSENT_COOKIES = _build_consent_cookies()


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
        # Use the avatar CDN directly instead of github.com/{owner}.png
        # (which used to 302 to this same CDN but has started returning
        # 403 for image requests). CDN URL is stable and always 200.
        return f"https://avatars.githubusercontent.com/{owner}?s=64"

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
    # Prefer site_url only when it's actually absolute (has a netloc).
    # Some feeds publish <link>/</link> which is "truthy" but has no
    # host — we must not treat that as the origin.
    parsed_site = urlparse(site_url or "")
    origin = parsed_site if parsed_site.netloc else urlparse(feed_url)
    if not origin.netloc:
        return ""
    return f"{origin.scheme or 'https'}://{origin.netloc}/favicon.ico"


def resolve_feed(url: str) -> dict:
    """Return {title, site_url, favicon_url, is_feed} for a feed URL.
    Never raises. `is_feed` reports whether the URL actually parsed as
    a feed (title in <channel>/<feed>, or at least one entry) — callers
    that want to distinguish "user pasted a homepage" from "valid feed
    with sparse metadata" use it to trigger discovery."""
    url = normalize_feed_url(url.strip())
    if not url:
        return {"title": "", "site_url": "", "favicon_url": "", "is_feed": False}

    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            cookies=_CONSENT_COOKIES,
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
    except Exception:
        return {
            "title": _fallback_title(url),
            "site_url": "",
            "favicon_url": resolve_avatar(url, "", feed_meta=None),
            "is_feed": False,
        }

    feed_meta = getattr(parsed, "feed", {}) or {}
    entries = getattr(parsed, "entries", None) or []
    # Stronger check than "has title or entries" — feedparser sets
    # `.version` to a non-empty tag ("rss20", "atom10", etc.) ONLY
    # when it identifies real feed markup. HTML pages that happen to
    # populate feed_meta.title (e.g. via og:title) leave version=""
    # and would previously slip through the old OR gate. Requiring
    # a recognized feed format catches those.
    detected_version = bool(getattr(parsed, "version", None))
    is_feed = detected_version or len(entries) > 0
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
        "is_feed": is_feed,
    }


# ------------------------------ Feed discovery ------------------------------
# When a user pastes a homepage URL instead of a feed URL, find candidate
# feeds by parsing <link rel="alternate"> and probing well-known paths.

_ALTERNATE_LINK_RE = re.compile(
    r'<link\b[^>]*\brel=["\']?alternate["\']?[^>]*>',
    re.IGNORECASE,
)
_LINK_ATTR_RE = re.compile(
    r'\b(type|href|title)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))',
    re.IGNORECASE,
)
_FEED_TYPES = {
    "application/rss+xml": "rss",
    "application/atom+xml": "atom",
    "application/feed+json": "json",
    "application/json": "json",
}

# Only tried when <link rel="alternate"> discovery finds nothing AND the
# input URL is "root-ish" (empty path or /). Blogger, WordPress, Ghost,
# Hugo, Jekyll all serve at least one of these.
_COMMON_FEED_PATHS = (
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/index.xml",
    "/feed.xml",
    "/feed.atom",
    "/feeds/posts/default",
)

_DISCOVERY_MAX_CANDIDATES = 10
_DISCOVERY_PROBE_TIMEOUT = 2.5


def _parse_link_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _LINK_ATTR_RE.finditer(tag):
        key = m.group(1).lower()
        val = m.group(2) or m.group(3) or m.group(4) or ""
        attrs[key] = val
    return attrs


def _looks_like_feed(body: bytes, content_type: str) -> bool:
    """Cheap sanity-check: content-type mentions xml/rss/atom/json OR the
    body starts with a plausible feed root element."""
    ct = (content_type or "").lower()
    if any(t in ct for t in ("xml", "rss", "atom", "feed+json", "application/json")):
        return True
    head = body[:512].lstrip().lower()
    return (
        head.startswith(b"<?xml")
        or b"<rss" in head
        or b"<feed" in head
        or (head.startswith(b"{") and b"jsonfeed" in body[:1024].lower())
    )


def _probe_candidate(url: str) -> tuple[bool, str]:
    """Try HEAD first, GET if the server refuses HEAD. Returns
    (is_feed, discovered_title). Title is best-effort — we peek at
    the first ~2KB and pull <title> if it's XML."""
    try:
        with httpx.Client(
            timeout=_DISCOVERY_PROBE_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            r = client.head(url)
            if r.status_code in (403, 405, 501) or (
                r.status_code == 200 and not (r.headers.get("content-type") or "")
            ):
                # Some servers 405 HEAD but 200 GET; retry with GET.
                r = client.get(url)
            if not (200 <= r.status_code < 300):
                return False, ""
            ct = r.headers.get("content-type", "")
            body = getattr(r, "content", b"") or b""
            if not body and r.status_code == 200:
                r = client.get(url)
                body = r.content
                ct = r.headers.get("content-type", ct)
            if not _looks_like_feed(body, ct):
                return False, ""
            title = ""
            m = re.search(rb"<title[^>]*>([^<]{0,200})</title>", body[:4096])
            if m:
                title = m.group(1).decode("utf-8", errors="replace").strip()
            return True, title
    except Exception:
        return False, ""


def discover_feeds(source_url: str) -> list[dict]:
    """Given an arbitrary URL, return candidate feeds found by:
      1. <link rel="alternate" type="application/rss+xml|atom+xml|feed+json">
      2. If none, well-known paths on a root-ish URL.
    Each result: {"url", "title", "type"}. Deduped, verified reachable,
    capped at _DISCOVERY_MAX_CANDIDATES."""
    if not source_url:
        return []
    source_url = source_url.strip()
    html = _http_get_text(source_url, timeout=FETCH_TIMEOUT)

    # Follow one hop of meta-refresh. Multilingual Jekyll/Hugo sites
    # commonly serve a tiny redirect page at the root that hops to
    # /fa/, /en/, etc.; the actual <link rel="alternate"> lives on
    # the target page. httpx handles HTTP redirects but not meta
    # refresh, so do that manually — bounded to a single hop to
    # avoid loops.
    if html:
        m = _META_REFRESH_RE.search(html)
        if m:
            target = m.group(1).strip().strip("'\"")
            hop = urljoin(source_url, target)
            if hop != source_url:
                target_html = _http_get_text(hop, timeout=FETCH_TIMEOUT)
                if target_html:
                    source_url = hop
                    html = target_html

    candidates: list[dict] = []
    seen: set[str] = set()

    def _add(url: str, title: str, kind: str) -> None:
        if not url:
            return
        # Normalize + drop fragments for dedup.
        url = urljoin(source_url, url).split("#", 1)[0]
        if url in seen or len(candidates) >= _DISCOVERY_MAX_CANDIDATES:
            return
        seen.add(url)
        candidates.append({"url": url, "title": title, "type": kind})

    if html:
        for tag in _ALTERNATE_LINK_RE.findall(html):
            attrs = _parse_link_attrs(tag)
            kind = _FEED_TYPES.get((attrs.get("type") or "").lower())
            if not kind:
                continue
            _add(attrs.get("href", ""), attrs.get("title", ""), kind)

    # Fallback: probe common paths if HTML discovery drew a blank.
    if not candidates:
        parsed = urlparse(source_url)
        # "Root-ish" heuristic: path is empty, "/", or a single segment.
        # Don't blindly probe /feed at the root when the user pasted a
        # subpath like https://example.com/blog/some-post.
        path = parsed.path or "/"
        if path in ("", "/") and parsed.netloc:
            base = f"{parsed.scheme or 'https'}://{parsed.netloc}"
            for suffix in _COMMON_FEED_PATHS:
                _add(base + suffix, "", "rss")

    # Verify each candidate is actually reachable + feed-shaped.
    verified: list[dict] = []
    for c in candidates:
        ok, discovered_title = _probe_candidate(c["url"])
        if not ok:
            continue
        if not c["title"]:
            c["title"] = discovered_title or _fallback_title(c["url"])
        verified.append(c)
    return verified
