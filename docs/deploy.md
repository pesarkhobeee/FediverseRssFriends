# Deploying rssRadar in production

The simplest workable stack: **one small VPS + Docker Compose + Caddy
for TLS + cron for backups**. This runs comfortably on a $5-6/mo box
(Hetzner CX22, DO, Vultr, etc.) and needs about 15 minutes to set up.

Alternative — running uvicorn under systemd on the host — is covered
briefly at the end.

## Prereqs

- A VPS with Docker + Docker Compose installed
- A domain (or subdomain) with DNS `A` record → your VPS IP
- SMTP credentials for sending magic-link emails
  - Fastmail, Mailgun, AWS SES, Postmark, ProtonMail Bridge — anything
    speaking SMTP works.

## 1. Get the code onto the box

```bash
ssh you@your-vps
git clone https://github.com/<you>/rssRadar.git
cd rssRadar
cp .env.example .env
```

## 2. Fill in `.env`

Bare minimum:

```
RSSRADAR_SESSION_SECRET=<paste 64 random chars>
RSSRADAR_BASE_URL=https://radar.example.com
RSSRADAR_SMTP_HOST=smtp.fastmail.com
RSSRADAR_SMTP_PORT=587
RSSRADAR_SMTP_USER=you@example.com
RSSRADAR_SMTP_PASSWORD=<app-specific password>
RSSRADAR_SMTP_FROM=rssRadar <no-reply@example.com>
```

Generate the session secret:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

**Never commit `.env`.** It's in `.gitignore` already.

## 3. Bring it up

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f app
```

Caddy handles TLS automatically the first time it starts — it'll fetch
a Let's Encrypt cert for the domain in `.env` and renew it forever.
DNS must be pointed at the VPS before this step.

Point a browser at `https://radar.example.com` — you should see the
landing page.

## 4. Set up daily backups

```bash
sudo crontab -e
```

Add:

```
15 3 * * * cd /home/you/rssRadar && ./scripts/backup.sh >> /var/log/rssradar-backup.log 2>&1
```

`scripts/backup.sh` runs a WAL-safe `sqlite3 .backup`, gzips, and
prunes to the newest 14 by default (`RSSRADAR_BACKUP_KEEP`). Log
grows about a line per day.

For offsite copies, add another cron line to `rsync` (or `restic`,
`borg`, `rclone` — pick your poison) the `backups/` directory to
S3/B2/a friend's VPS.

## 5. First admin

The first user to sign up on a fresh instance is your admin (once we
build the admin surface planned in `docs/instance-config.md`). For
now: sign up with your normal email; that's your account.

## Updating

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

The startup DB migration runs on every boot and is idempotent, so
column-additions from future versions are automatic.

## Monitoring

- `GET /healthz` returns `{"ok": true}` — point a free uptime checker
  (UptimeRobot, BetterStack) at `https://radar.example.com/healthz`.
- `docker compose logs -f app` for application logs (uvicorn's stdout).
- Caddy access log lives inside its container by default; enable a
  file sink in `Caddyfile` if you want long-term access logs.

## Storage sizing

At MVP scale everything fits in a few hundred MB — one user with 300
feeds averaging 100 items retained produces roughly 30 MB. The
`RSSRADAR_FEED_ITEM_KEEP` env var caps items-per-feed (default 100)
so growth is bounded.

## Resource budget

- **RAM**: ~120-200 MB for the Python process at rest, spikes to
  ~400 MB during a refresh cycle with 10 concurrent workers.
- **CPU**: idle 99% of the time; a refresh cycle uses a fraction of
  one core for ~30-60s every `REFRESH_INTERVAL_MINUTES` (default 15).
- **Network**: outbound to feed URLs during refresh, plus SMTP for
  magic links. Inbound: only browser + feed reader requests.
- **Disk**: DB grows slowly; cap it by pruning old FeedItems if
  ever needed.

A 1 vCPU / 1 GB VPS handles this with room to spare.

## Systemd alternative

If Docker isn't your thing, run uvicorn under systemd. `uv sync` on
the host, then:

```ini
# /etc/systemd/system/rssradar.service
[Unit]
Description=rssRadar
After=network.target

[Service]
User=rssradar
WorkingDirectory=/srv/rssradar
EnvironmentFile=/srv/rssradar/.env
ExecStart=/srv/rssradar/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then a Caddy reverse-proxy block pointing at 127.0.0.1:8000 (same
shape as the Docker version below). Backups still work — cron just
calls the same script.

## Security notes

- **Session secret must be secret**. Anyone who has it can forge
  session cookies. Rotate it (rewriting `.env` and restarting) if
  you suspect it leaked; it invalidates all sessions.
- **HTTPS is not optional** in production. Magic-link tokens travel
  in URLs; sending them over HTTP means someone on the network path
  can hijack an account.
- **SMTP creds**: use app-specific passwords / limited tokens, never
  your primary email password.
- **Rate limits on `/login`** exist (`RSSRADAR_MAGIC_LINK_MIN_INTERVAL`,
  default 60s per email). Consider putting the app behind Cloudflare
  or fail2ban for broader abuse protection.
- **Public radars are public**. Search engines will index them
  eventually. If you want that off, set `visibility='private'` per
  user — the visibility gate is enforced everywhere.

## What's NOT covered here (yet)

- **Federation** — parked in `docs/federation-activitypub.md`. When
  we ship it, this doc grows a "point a WebFinger-capable subdomain
  here" section.
- **Instance admin UI** — planned in `docs/instance-config.md`.
  Until then, instance name and other Tier 2 settings live in env
  vars and take effect on restart.
- **Multi-node / horizontal scaling** — SQLite pins you to one
  machine. If you outgrow that, migrate to Postgres (single
  `DATABASE_URL` change) and put uvicorn behind a load balancer.
  Not needed until you're serving thousands of concurrent users.
