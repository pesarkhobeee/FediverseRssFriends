# rssFriends

Multi-user, self-hosted RSS radar. Sign in with a magic link, curate feeds by category, share your radar publicly, follow other users, star items, and expose your activity as an RSS feed.

Stack: **FastAPI + HTMX + Jinja2 + SQLite (WAL) + APScheduler**, managed with **uv**. No JS build step, no Redis, no worker process. One Docker container.

Visual language is inherited from [lovelyRSS](https://github.com/pesarkhobeee/lovelyRSS): warm-gray surface, olive-green accent for active category chips and "New" badges, full-width card rows with favicon + title + "Updated X ago". See `app/static/style.css` for the palette.

## Status

Steps 1–7 done. Every item on the radar can be **starred**; `/u/{handle}/favorites` lists what a user has starred (respects visibility). Star toggles are single HTMX swaps — the button re-renders in place. Signed-in visitors see the correct star state for every item on someone else's radar. Follow + activity land in the next steps.

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
uv run uvicorn app.main:app --reload   # http://localhost:8000
uv run pytest -q
uv run ruff check .
```

Or via the Makefile:

```bash
make install
make dev
make test
make lint
```

Docker:

```bash
docker compose up --build
```

## Roadmap

1. Skeleton ✅
2. Magic-link auth ✅
3. Dashboard CRUD: categories + subscriptions ✅
4. Background fetcher + `FeedItem` cache ✅
5. Public `/u/{handle}` + visibility gate (public / unlisted / private) ✅
6. Cherry-pick + fork + OPML import ✅
7. Favorites (★ items) ✅
8. Follow (auto-approve; pending requests for private users) ✅
9. Activity log + `/u/{handle}/activity` + `activity.rss` (signed token for private) ✅
10. Home timeline `/feed` ✅
11. OPML import/export ✅ *(shipped in step 6)*
12. Discover ✅ *(you are here)*
13. Polish (favicon cache, health badges, rate limits, backup)

## Config

Environment variables:

- `RSSFRIENDS_DATABASE_URL` — default `sqlite:///./data/rssfriends.db`
- `RSSFRIENDS_DATA_DIR` — default `./data`
- `RSSFRIENDS_SESSION_SECRET` — **change in production**
- `RSSFRIENDS_DEBUG` — `1` to enable debug behavior
- `RSSFRIENDS_BASE_URL` — public URL used in magic-link emails (default `http://localhost:8000`)
- `RSSFRIENDS_MAGIC_LINK_TTL_MINUTES` — token lifetime (default `15`)
- `RSSFRIENDS_MAGIC_LINK_MIN_INTERVAL` — min seconds between links per email (default `60`)
- `RSSFRIENDS_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` / `_STARTTLS` — leave `SMTP_HOST` empty for dev mode (link printed to stdout)
- `RSSFRIENDS_ENABLE_SCHEDULER` — `1` (default) starts the background refresh loop; set to `0` for one-shot / test runs
- `RSSFRIENDS_REFRESH_INTERVAL_MINUTES` — cycle interval (default `15`)
- `RSSFRIENDS_REFRESH_BATCH_SIZE` — max feeds refreshed per cycle (default `50`)
- `RSSFRIENDS_FEED_ITEM_KEEP` — max items retained per feed (default `100`)

## Backup + restore

The SQLite file lives at `./data/rssfriends.db` by default. Two scripts handle
WAL-safe backups with a rolling retention window:

```bash
make backup                         # snapshot → ./backups/rssfriends-<ts>.sqlite.gz
RSSFRIENDS_BACKUP_KEEP=30 make backup # keep 30 archives instead of the default 14
make restore FROM=backups/rssfriends-20260101-030000.sqlite.gz
```

Backups use `sqlite3 .backup`, which is WAL-safe (a plain `cp` would tear if
the app is writing). Each archive is gzip-tested before it's counted as a
keeper, and pruning only touches files matching the exact naming pattern —
your own files in `backups/` are left alone.

For cron on the host:

```
15 3 * * * cd /srv/rssfriends && ./scripts/backup.sh >> /var/log/rssfriends-backup.log 2>&1
```

`restore.sh` moves the existing DB aside (`*.bak-<ts>`) before overwriting,
so an accidental restore never destroys data.

## Admin queries

There's no admin UI yet (`docs/instance-config.md` outlines the plan).
Meanwhile, poke at the SQLite file directly. The DB lives at
`data/rssfriends.db` locally, `/srv/rssfriends/data/rssfriends.db` on the
production host. All queries below assume you're on the host — prepend
`ssh root@your-host` and quote the whole `sqlite3 …` invocation if you're
running remotely.

### How many users?

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT
  COUNT(*)                            AS total,
  SUM(visibility='public')            AS public,
  SUM(visibility='unlisted')          AS unlisted,
  SUM(visibility='private')           AS private
FROM user;
"
```

### Per-user breakdown (feeds, stars, follower graph)

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT
  u.handle,
  u.email,
  u.visibility,
  substr(u.created_at, 1, 16) AS joined,
  (SELECT COUNT(*) FROM subscription s WHERE s.user_id = u.id) AS feeds,
  (SELECT COUNT(*) FROM favorite   f WHERE f.user_id = u.id) AS stars,
  (SELECT COUNT(*) FROM follow WHERE followee_id = u.id AND approved_at IS NOT NULL) AS followers,
  (SELECT COUNT(*) FROM follow WHERE follower_id = u.id AND approved_at IS NOT NULL) AS following
FROM user u
ORDER BY u.created_at;
"
```

### Fleet-wide feed health

Which feeds are failing right now?

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT substr(url, 1, 70) AS url, consecutive_failures AS fails, substr(last_error, 1, 40) AS err
FROM feed
WHERE consecutive_failures > 0
ORDER BY consecutive_failures DESC
LIMIT 20;
"
```

### Recent activity across the instance

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT
  substr(a.created_at, 1, 16) AS at,
  u.handle,
  a.kind,
  substr(a.subject, 1, 40) AS subject
FROM activity a
JOIN user u ON u.id = a.user_id
ORDER BY a.created_at DESC
LIMIT 20;
"
```

### Grant / revoke admin status

`is_admin` isn't a column yet (planned in `docs/instance-config.md`). For
now, "admin" is *whoever has SSH access to the box*.

### Safety notes

- `sqlite3` on a live DB is safe for `SELECT` — WAL mode means readers
  don't block writers. UPDATEs should still be preceded by a backup:
  `./scripts/backup.sh` first.
- For destructive changes (delete a user, purge failing feeds), always
  run `BEGIN; … ROLLBACK;` first to preview the row count. Then re-run
  with `COMMIT;`.

## License

MIT
