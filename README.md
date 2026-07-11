# rssRadar

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

- `RSSRADAR_DATABASE_URL` — default `sqlite:///./data/rssradar.db`
- `RSSRADAR_DATA_DIR` — default `./data`
- `RSSRADAR_SESSION_SECRET` — **change in production**
- `RSSRADAR_DEBUG` — `1` to enable debug behavior
- `RSSRADAR_BASE_URL` — public URL used in magic-link emails (default `http://localhost:8000`)
- `RSSRADAR_MAGIC_LINK_TTL_MINUTES` — token lifetime (default `15`)
- `RSSRADAR_MAGIC_LINK_MIN_INTERVAL` — min seconds between links per email (default `60`)
- `RSSRADAR_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` / `_STARTTLS` — leave `SMTP_HOST` empty for dev mode (link printed to stdout)
- `RSSRADAR_ENABLE_SCHEDULER` — `1` (default) starts the background refresh loop; set to `0` for one-shot / test runs
- `RSSRADAR_REFRESH_INTERVAL_MINUTES` — cycle interval (default `15`)
- `RSSRADAR_REFRESH_BATCH_SIZE` — max feeds refreshed per cycle (default `50`)
- `RSSRADAR_FEED_ITEM_KEEP` — max items retained per feed (default `100`)

## Backup + restore

The SQLite file lives at `./data/rssradar.db` by default. Two scripts handle
WAL-safe backups with a rolling retention window:

```bash
make backup                         # snapshot → ./backups/rssradar-<ts>.sqlite.gz
RSSRADAR_BACKUP_KEEP=30 make backup # keep 30 archives instead of the default 14
make restore FROM=backups/rssradar-20260101-030000.sqlite.gz
```

Backups use `sqlite3 .backup`, which is WAL-safe (a plain `cp` would tear if
the app is writing). Each archive is gzip-tested before it's counted as a
keeper, and pruning only touches files matching the exact naming pattern —
your own files in `backups/` are left alone.

For cron on the host:

```
15 3 * * * cd /srv/rssradar && ./scripts/backup.sh >> /var/log/rssradar-backup.log 2>&1
```

`restore.sh` moves the existing DB aside (`*.bak-<ts>`) before overwriting,
so an accidental restore never destroys data.

## License

MIT
