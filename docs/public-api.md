# Public read API (`/api/v1`)

Status: planned. Slice 1 shipped in 0.11.53.

## Goal

A small, read-only JSON API over data that is already public on the
HTML pages, so people can use rssFriends from a terminal or script:

```bash
curl -s https://rssfriends.com/api/v1/users/farid/items | jq '.items[].title'
```

Primary consumer: CLI tools and scripts. No browser, no cookies, no
JavaScript.

## Decisions

- **Separate sub-app** mounted at `/api/v1`, with its own
  `/api/v1/openapi.json` and `/api/v1/docs`
  ([FastAPI sub-applications](https://fastapi.tiangolo.com/advanced/sub-applications/)).
- **Main app schema off.** Today `/docs` and `/openapi.json` publish
  all 87 internal HTMX routes, including write routes. Set
  `docs_url=None, redoc_url=None, openapi_url=None` on the main
  `FastAPI(...)` ([conditional OpenAPI](https://fastapi.tiangolo.com/how-to/conditional-openapi/)).
- **Read-only, anonymous.** `GET` only. The API never reads the
  session cookie, so every response is identical for every caller
  and safe to cache publicly.
- **Visibility:** every endpoint resolves the target user and checks
  `can_view(None, target)`.
  - `public` → listed and fetchable.
  - `unlisted` → fetchable by handle (so a CLI can read a radar whose
    handle it knows), never returned by list endpoints.
  - `private` → 404, same as an unknown handle (no existence oracle).
- **Local users only** in v1. Remote ActivityPub actors are out of
  scope.
- **Explicit response models.** Pydantic models with an allowlist of
  fields. Never return SQLModel table rows: a `User` row would
  serialize `email` and `activity_secret`.
- **Stable identifiers:** handles and collection slugs; numeric ids
  only for feeds and items, which already have public URLs (`/f/{id}`,
  `/i/{id}`).

## Endpoints

| Endpoint | Data | Visibility |
|---|---|---|
| `GET /instance` | name, version, user/feed counts | — |
| `GET /users?cursor=` | handle, avatar, counts | `public` only |
| `GET /users/{handle}` | profile + counts | `public`, `unlisted` |
| `GET /users/{handle}/categories` | name, feed count | same |
| `GET /users/{handle}/feeds?category=` | feed title, url, site_url, favicon | same |
| `GET /users/{handle}/items?category=&cursor=` | merged newest-first stream | same |
| `GET /users/{handle}/bookmarks?cursor=` | bookmarked items | same |
| `GET /users/{handle}/activity?cursor=` | activity log | same |
| `GET /users/{handle}/followers`, `/following` | handles | same, minus private users (see open questions) |
| `GET /collections`, `/collections/{slug}` | public collections + feeds | `is_public` only |
| `GET /feeds/{id}`, `/feeds/{id}/items?cursor=` | one feed | public |

Pagination: opaque cursor encoding `(published_at, id)`, `limit`
default 50, max 100. Response shape: `{"items": [...], "next": "<cursor>|null"}`.

## Never exposed

`User.email`, `User.activity_secret`, `Subscription.note` (owner's
private note), anything on `WallabagAccount`, pending `Follow` rows,
`MagicToken`, private users' categories/feeds/items, linked private
categories (0.11.52).

## Risks and guards

| Risk | Example | Guard |
|---|---|---|
| New column leaks | Someone adds `User.phone`; a model built with `**row.dict()` starts returning it | Allowlist models + a snapshot test of `/api/v1/openapi.json`: any schema change fails CI until the snapshot is updated on purpose |
| Private user via a list | Public alice's `/following` returns private bob → reveals bob exists and whom he follows | Filter `visibility == "private"` out of every list |
| Cached personal data | API reads the session; Cloudflare caches farid's response and serves it to everyone | API never touches the session; responses carry `Cache-Control: public, max-age=300` |
| Scraping load | A script polls `/items` for every user each minute; SQLite contention delays feed refresh | CDN cache above, plus a per-IP limit (`limit_req` on the host nginx or `slowapi`) |
| Cross-site use with cookies | Another site calls the API with a visitor's cookies | CORS `Access-Control-Allow-Origin: *`, `GET` only, no credentials |
| Handle renamed | CLI config pinned to an old handle | Old handle 301s to the canonical one, same as HTML (`maybe_redirect_to_canonical`) |

## Slices

1. Turn off main-app `/docs`, `/redoc`, `/openapi.json`.
2. `/api/v1` sub-app: `instance`, `users/{handle}`, `categories`,
   `feeds` + leak tests (private 404, unlisted not listed, no
   forbidden fields in any response) + schema snapshot test.
3. `items`, `bookmarks`, `activity`, `collections`, cursor pagination.
4. Cache headers + rate limit.
5. Later: personal access tokens so a CLI can read the caller's own
   private radar. Read-only scope, stored hashed, revocable from
   Settings. A token pasted into a dotfile or shell history is a
   credential leak, so tokens must never grant write access.

## Settled questions

- HTML `/u/{handle}/followers` and `/following` keep listing private
  users (same as Mastodon with locked accounts). Only the API filters
  them out, so it never hands out a machine-readable list of private
  handles.
- No official CLI. The API is documented with `curl | jq` recipes.

## CLI recipes

```bash
API=https://rssfriends.com/api/v1

# Newest 10 item titles + links from a radar
curl -s "$API/users/farid/items?limit=10" | jq -r '.items[] | "\(.title)\t\(.link)"'

# One category only
curl -s "$API/users/farid/items?category=Tech" | jq -r '.items[].title'

# Feed URLs of a radar (e.g. to import elsewhere)
curl -s "$API/users/farid/feeds" | jq -r '.items[].url'

# Follow pagination until the end
url="$API/users/farid/items"
while [ -n "$url" ]; do
  page=$(curl -s "$url")
  echo "$page" | jq -r '.items[].title'
  next=$(echo "$page" | jq -r '.next // empty')
  url=${next:+"$API/users/farid/items?cursor=$next"}
done
```
