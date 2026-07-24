# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What rssFriends is

**A social radar for the open web** — not a reader. No unread counters, no backlog, no algorithm. Users *watch* feeds and people they trust; they don't clear a queue. Every URL is subscribable, every export is OPML, and the roadmap points at ActivityPub. When writing copy or naming things, prefer **radar / watch / catch / signal / feeds / friends** over **reader / read / inbox / articles / subscribers**. See `docs/design-language.md` for the full voice notes.

## Commands

**Package + venv management via `uv`.** All dev commands go through it.

```bash
uv sync --all-extras                        # install (creates .venv)
uv run uvicorn app.main:app --reload        # dev server → http://localhost:8000
uv run pytest -q                            # all tests
uv run pytest tests/test_public.py::test_public_radar_shows_categories_and_feeds -vv
uv run ruff check .                         # lint
uv run ruff check --fix .                   # autofix
```

Or the Makefile aliases: `make install / dev / test / lint / fmt / up / down`.

**Backup + restore** (WAL-safe via `sqlite3 .backup`):
```bash
make backup
make restore FROM=backups/rssfriends-<ts>.sqlite.gz
```

**Version bump** — edit `app/__init__.py` (`__version__`) *and* `pyproject.toml`. The version is baked into static asset URLs (`?v={version}`) so bumps also bust Cloudflare's edge cache.

## Deployment (production)

Two compose files:
- `docker-compose.yml` — plain dev.
- `docker-compose.prod.yml` — with a Caddy sidecar for TLS.
- On the shared `pathseeker` host we use `docker-compose.pathseeker.yml` (bound to `127.0.0.1:8300`, TLS handled by the host's nginx; that file is NOT in git). Deploy via `make deploy` — runs tests + lint, `rsync`s to `root@pathseeker:/srv/rssfriends/`, rebuilds the container, and verifies `/version`. Use `make deploy-check` for a dry-run. See `docs/deploy.md` for the manual/first-time setup. **Never call `rsync --delete` by hand** — the Makefile's exclude list has trip-wire history (once wiped prod `.env`).

## Architecture

FastAPI + HTMX + Jinja2 + SQLite (WAL) + APScheduler, one Docker container, no JS build step.

### Data model (`app/db.py`)

Eight tables. Every join is one hop; every FK is an integer.

- **`User`** — magic-link users. Fields: `email`, `handle`, `visibility` ∈ `{public, unlisted, private}`, `activity_secret` (per-user HMAC key for signing private activity.rss URLs).
- **`MagicToken`** — single-use auth tokens with a short TTL.
- **`Category`** — user-scoped; the label a user groups feeds under.
- **`Feed`** — **global** row deduped by URL. Every user subscribed to the same URL shares this row. Has `favicon_url`, `last_fetched_at`, `consecutive_failures`, `last_error`.
- **`Subscription`** — a (user, feed, category) triple. Unique per (user, feed).
- **`FeedItem`** — items parsed from a Feed. Dedup by `(feed_id, guid)`. `title` can be derived from summary if the source omits `<title>` (Mastodon).
- **`Favorite`** — composite PK `(user_id, feed_item_id)` — physically deduped by the DB, not by app logic.
- **`Follow`** — composite PK `(follower_id, followee_id)`. `approved_at IS NULL` = pending request (private followees only); non-NULL = approved.
- **`Activity`** — append-only log. `kind` ∈ `{subscribed, favorited, followed, forked, created_category}`. `subject` / `subject_url` are **denormalized** so the timeline still renders after the referenced row is deleted.
- **`WallabagAccount`** — one-per-user OAuth link to a self-hosted [Wallabag](https://wallabag.org). Bookmarking an item fires a background POST to `/api/entries.json`. Wallabag only supports the `password` grant, so we store the user's Wallabag username + password too. Sensitive fields (`password`, `client_secret`, `access_token`, `refresh_token`) are **encrypted at rest** — see below.

**Never write raw values into `Feed.favicon_url` / `Feed.title` / `Feed.url` without going through the helpers** — see below.

### Secrets & field encryption (`app/crypto.py`)

**One env var, `RSSFRIENDS_SESSION_SECRET`, does three unrelated jobs.** Rotating it invalidates all three at once — that's intentional but load-bearing:

1. Signs Starlette session cookies (`app/main.py`) — rotation logs everyone out.
2. Mixed with `User.activity_secret` when signing `activity.rss` URLs on private radars.
3. Via HKDF-SHA256 (salt `"rssfriends/field-encryption/v1"`) derives a 32-byte Fernet key that encrypts sensitive `WallabagAccount` fields at rest (`app/crypto.py`, `encrypt()`/`decrypt()`). Rotation makes every stored Wallabag credential unrecoverable — `decrypt()` returns `""` on `InvalidToken` so callers can re-prompt safely instead of leaking plaintext.

Threat model for field encryption: a stolen DB dump alone is opaque (backup theft, cloud snapshot). A full server compromise still exposes creds, since the running app must be able to decrypt what it encrypted — that's inherent, not a bug.

When adding a new sensitive field to a model, run it through `crypto.encrypt()` on write and `crypto.decrypt()` on read. Never assert equality against raw storage in tests — assertions use `decrypt()`.

### Feed URL + metadata pipeline

`app/feeds.py` is the choke point for anything URL- or avatar-related:

- **`normalize_feed_url(url)`** — rewrites `github.com/user` → `github.com/user.atom` and `reddit.com/r/x/` → `reddit.com/r/x/.rss`. Called on every ingest path (subscribe, cherry-pick, OPML import).
- **`resolve_feed(url)`** — network fetch; returns `{title, site_url, favicon_url}`. For GitHub feeds, `title` is overridden to the username (upstream ships a generic "Github Public timeline feed").
- **`resolve_avatar(url, site_url, feed_meta, deep=bool)`** — priority: GitHub owner → YouTube channel og:image (deep only) → feed's own `<image>` → HTML `<link rel="icon">` (deep only) → `favicon.ico`. Refresh cycle uses `deep=False` (no extra network); `resolve_feed` and manual per-feed refresh use `deep=True`.
- **`github_owner(url)`** — extracts a valid user/org name from any github.com URL shape. Rejects non-user paths (`orgs/`, `topics/`, etc.).

### Visibility gate — one function guards everything

**`app.public.can_view(viewer, target, db)`** returns True iff:
- target is `public` or `unlisted`, or
- viewer is the target, or
- viewer has an approved `Follow` row against the target.

Every route that could leak a private radar — `/u/{handle}`, `.opml`, `.rss`, `/activity`, `/activity.rss`, `/favorites`, `/followers`, `/following` — calls `can_view()`. **Never bypass it.** The HTML `/u/{handle}` route falls back to `user_radar_locked.html` with a Follow button instead of a raw 403.

### Background refresh (`app/refresh.py`)

Runs in an in-process `BackgroundScheduler`. `run_cycle()` uses a `ThreadPoolExecutor` (`REFRESH_CONCURRENCY`, default 10). Each worker opens its own `Session`; SQLite WAL + `check_same_thread=False` makes concurrent writes safe at MVP scale.

Manual per-feed refresh (`POST /feeds/{id}/refresh`) additionally re-runs `resolve_avatar(deep=True)` — that's when a subscribed YouTube channel's avatar or a blog's `<link rel="icon">` gets picked up.

Kicking `kick_background_refresh()` after OPML import so newly-added feeds populate items immediately.

### Signed tokens (private RSS feeds)

`/u/{handle}/activity.rss` and `/u/{handle}.rss` for private users require `?as={follower_handle}&t={hmac}`. HMAC is `sha256(follower.id, key=owner.activity_secret)[:16]`, generated by `app.activity.sign_follower`. Signed URLs are baked into the hero for approved followers automatically. Revoking a follower makes the same HMAC still verify but the `Follow` row lookup fails → 403.

### Auth (`app/auth.py` + `app/mail.py`)

Magic-link only. Session cookie via `SessionMiddleware`. Login POST → mint token → `send_magic_link` picks its delivery path in this order:
1. Resend HTTP API (`RSSFRIENDS_RESEND_API_KEY`)
2. SMTP (`RSSFRIENDS_SMTP_HOST`)
3. stdout dev-mode (both blank)

Handle is derived from email local-part with `-2`, `-3`, … collision suffixing. Rate-limit: one link per email per 60 s.

### HTMX conventions

- Every state-changing route returns a small HTML **partial**, not JSON. Templates prefixed `_` are partials.
- `hx-swap="outerHTML"` for toggles (star, follow button).
- `hx-swap="delete"` for removals; endpoint returns empty 200.
- `hx-swap="beforeend"` for appends (new category card, new feed row).
- Global error toast + inbound-4xx/5xx handlers live in `base.html`. Distinguish `htmx:sendError` from `htmx:responseError` — the former is a network failure.

### Templates

- All static assets in `base.html` carry `?v={{ app_version }}` so a version bump busts Cloudflare's edge cache automatically.
- **Jinja gotcha**: never name a template context key `items`. Jinja's `.items` resolves to `dict.items()` (a bound method), silently. We hit this twice; use `subs`, `entries`, etc.
- `feed-card` partial (`_public_feed_card.html`) is reused by both the flat "All feeds" view and the per-category filtered view on `/u/{handle}`.
- Category chips filter the public radar via `?cat=…`. Empty filter → flat cross-category sort by newest item.

### Migrations

`init_db()` runs on startup. In addition to `SQLModel.metadata.create_all`, we run a set of **idempotent SQLite migrations** — `ALTER TABLE ADD COLUMN` when a column is missing, then a data backfill. Each backfill is a `_backfill_*` function in `db.py`. Adding a new one: check idempotency (the second boot should touch zero rows).

Currently: `activity_secret` on users, `favicon_url` for existing GitHub rows, URL normalization for legacy `github.com/user` / `reddit.com/r/x/`, GitHub feed titles, Mastodon `(untitled)` items with derivable summaries.

**Never `DROP TABLE` in a migration.** Not until we build a proper migration tool.

### Testing

`tests/conftest.py` sets env vars **before** any `app.*` import — the config module captures env at import time. Per-test fixture drops and recreates the schema.

Delivery is stubbed for tests: `RSSFRIENDS_ENABLE_SCHEDULER=0` in conftest. Network calls go through `feeds._http_get_text`, `refresh.httpx.Client`, and `resend.Emails.send` — stub with monkeypatch (`_stub_httpx`, `_stub_html_fetch`, `_stub_resolve` helpers).

Handle-derivation for login helper: `_login(client, "alice@e.com")` creates a user with handle `alice`.

## Planning docs (design decisions parked)

`docs/` contains design docs for features we haven't built yet. **Read the relevant doc before starting any of these** — they answer schema/UX questions that already got hashed out:

- `federation-activitypub.md` — full AP plan mapped against v0.2.1's schema. Actor JSON-LD, six new tables, five slices with LOC estimates.
- `instance-config.md` — hybrid config surface (env vars for bootstrap, `InstanceSetting` table + `/admin` UI for runtime). Prerequisite for federation.
- `attribution-chain.md` — curator-credit graph (`source_kind`, `source_user_id` on `Subscription`).
- `deploy.md` — VPS deployment walkthrough (Docker Compose + Caddy + cron).

## Style guardrails I've been bitten by

- **Deduplication belongs in the schema when possible.** `Favorite`, `Follow`, `Feed` (via unique `url`) all use PK/unique constraints so app bugs can't create duplicates.
- **Never mint a URL that we can't renumber later.** `/i/{item_id}` is the exception (federation needs stable IDs); everything else routes by handle or session.
- **Don't add a new template's context dict key called `items`** (see Jinja gotcha above).
- **Cache-bust on version bump** — every static asset in `base.html` uses `?v={{ app_version }}`. If you add a new asset, follow suit.
- **Any new write site that emits an Activity should also skip logging OPML bulk imports** to avoid flooding a follower's timeline with hundreds of `subscribed` events.

## Roadmap position

The README's numbered steps 1–12 are shipped. "Polish" (step 13) is ongoing — favicon cache on disk, per-feed health badges, rate limits per remote host, admin/config surface. Federation (docs/federation-activitypub.md) is the next big block; instance-config (docs/instance-config.md) is a prerequisite.
