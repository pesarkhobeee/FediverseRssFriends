# rssRadar

Multi-user, self-hosted RSS radar. Sign in with a magic link, curate feeds by category, share your radar publicly, follow other users, star items, and expose your activity as an RSS feed.

Stack: **FastAPI + HTMX + Jinja2 + SQLite (WAL) + APScheduler**, managed with **uv**. No JS build step, no Redis, no worker process. One Docker container.

Visual language is inherited from [lovelyRSS](https://github.com/pesarkhobeee/lovelyRSS): warm-gray surface, olive-green accent for active category chips and "New" badges, full-width card rows with favicon + title + "Updated X ago". See `app/static/style.css` for the palette.

## Status

Step 1 of the build order — **skeleton only**. Landing page, session middleware, SQLite bootstrap, health check. Auth, feeds, follow, and activity land in later steps.

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

1. Skeleton ← *you are here*
2. Magic-link auth
3. Dashboard CRUD: categories + subscriptions
4. Background fetcher + `FeedItem` cache
5. Public `/u/{handle}` + visibility gate (public / unlisted / private)
6. Cherry-pick + fork another user's radar
7. Favorites (★ items)
8. Follow (auto-approve; pending requests for private users)
9. Activity log + `/u/{handle}/activity` + `activity.rss` (signed token for private)
10. Home timeline `/feed`
11. OPML import/export
12. Discover
13. Polish (favicon cache, health badges, rate limits, backup)

## Config

Environment variables:

- `RSSRADAR_DATABASE_URL` — default `sqlite:///./data/rssradar.db`
- `RSSRADAR_DATA_DIR` — default `./data`
- `RSSRADAR_SESSION_SECRET` — **change in production**
- `RSSRADAR_DEBUG` — `1` to enable debug behavior

## License

MIT
