# Configuration reference

Every knob is an environment variable read at startup. Set them in `.env`
(copied from `.env.example`) or export them on the shell.

## Core

- `RSSFRIENDS_DATABASE_URL` — default `sqlite:///./data/rssfriends.db`
- `RSSFRIENDS_DATA_DIR` — default `./data`
- `RSSFRIENDS_SESSION_SECRET` — **change in production.** This one env var
  carries a lot of weight:
  - signs Starlette session cookies (rotating logs everyone out)
  - mixed with `User.activity_secret` for signed `activity.rss` URLs on
    private radars
  - via HKDF-SHA256 (salt `"rssfriends/field-encryption/v1"`) derives the
    **Fernet key that encrypts Wallabag credentials at rest** in the DB
    (`password`, `client_secret`, `access_token`, `refresh_token`) —
    see `app/crypto.py`
  - **Rotation implication:** changing this value invalidates every
    stored Wallabag credential (users have to reconnect) *and* logs
    everyone out. Intentional — that's the correct behavior for a
    stolen-secret scenario. Only rotate if you actually believe the
    secret has leaked.
  - Generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`.
- `RSSFRIENDS_DEBUG` — `1` to enable debug behavior
- `RSSFRIENDS_BASE_URL` — public URL used in magic-link emails
  (default `http://localhost:8000`)

## Auth

- `RSSFRIENDS_MAGIC_LINK_TTL_MINUTES` — token lifetime (default `15`)
- `RSSFRIENDS_MAGIC_LINK_MIN_INTERVAL` — min seconds between links per
  email (default `60`)
- `RSSFRIENDS_BLOCKED_EMAIL_DOMAINS` — comma-separated domains that can
  neither sign up nor sign in (subdomains included). Checked when a link
  is requested and again when it's used, so links already sent stop
  working. Existing accounts are locked out, not deleted.

## Email

- `RSSFRIENDS_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` /
  `_STARTTLS` — leave `SMTP_HOST` empty for dev mode (link printed to
  stdout)

## Background scheduler

- `RSSFRIENDS_ENABLE_SCHEDULER` — `1` (default) starts the background
  refresh loop; set to `0` for one-shot / test runs
- `RSSFRIENDS_REFRESH_INTERVAL_MINUTES` — cycle interval (default `15`)
- `RSSFRIENDS_REFRESH_BATCH_SIZE` — max feeds refreshed per cycle
  (default `50`)
- `RSSFRIENDS_FEED_ITEM_KEEP` — max items retained per feed
  (default `100`)

## Seed collections

- `RSSFRIENDS_SEED_FILE` — JSON file of official collections created
  and synced on startup (default `instance/collections.json`; missing
  file = no seed collections). Format: [`instance/README.md`](../instance/README.md)
- `RSSFRIENDS_SEED_COLLECTIONS` — `0` skips seeding entirely
  (default `1`)

## Instance identity

See [`docs/instance-config.md`](instance-config.md) for the full list
of `RSSFRIENDS_INSTANCE_*` knobs (name, tagline, admin handles, open
registration, etc.).
