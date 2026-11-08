# Self-hosting rssFriends

You can run your own rssFriends instance in about 15 minutes on any
VPS with Docker. This doc walks you through the whole thing — DNS,
first boot, admin setup, federation health checks, backup story.

The primary instance at [rssfriends.com](https://rssfriends.com) is
just *one* rssFriends node. You can run your own with your own name,
tagline, admin contact, and community. All the pieces of the app
(WebFinger, ActivityPub actor, inbox, outbox, delivery) work across
instances — a user on `you.example` can follow `@farid@rssfriends.com`
and vice versa.

## What you need

- A **VPS** with Docker + Docker Compose (any Linux distro; 1 GB RAM
  is plenty for a small instance)
- A **domain** you control (e.g. `rss.mydomain.com`)
- A **Resend** account for magic-link email delivery
  ([resend.com](https://resend.com) — free tier is fine at any
  reasonable scale). SMTP works too if you'd rather.

## Step 1 · Point DNS at your VPS

Create an **A** record for your domain pointing at your VPS IP.
Wait until `dig +short rss.mydomain.com` returns the right IP before
continuing.

If you're behind Cloudflare, set the record to **DNS-only** (grey
cloud) for the first boot so Caddy can complete the HTTP-01 ACME
challenge. You can flip to proxied afterwards.

## Step 2 · Clone + configure

```bash
git clone https://github.com/pesarkhobeee/FediverseRssFriends.git /srv/rssfriends
cd /srv/rssfriends
cp .env.example .env
```

Open `.env` and fill in **at least** these five:

```dotenv
RSSFRIENDS_HOSTNAME=rss.mydomain.com
RSSFRIENDS_BASE_URL=https://rss.mydomain.com

RSSFRIENDS_INSTANCE_NAME=My Radar
RSSFRIENDS_INSTANCE_TAGLINE=A cosy corner of RSS

RSSFRIENDS_SESSION_SECRET=<paste a fresh secret>
```

Generate the secret with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Then set your email backend (`RSSFRIENDS_RESEND_API_KEY` +
`RSSFRIENDS_RESEND_FROM`) so magic links actually reach people. Leave
both blank for dev mode — links print to `docker logs` instead.

## Step 3 · First boot

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Caddy will fetch a Let's Encrypt cert for your hostname on first hit.
Watch it complete with:

```bash
docker compose -f docker-compose.prod.yml logs -f caddy
```

Once you see `certificate obtained successfully`, hit
`https://rss.mydomain.com/version` from any machine. You should see
JSON like `{"version": "0.11.34"}`.

## Step 4 · Sign yourself up + become admin

Visit `https://rss.mydomain.com` and enter your email. Click the
magic link and you'll land on your radar page.

**The first-registered user is automatically an admin**, so you can
create collections, moderate suggestions, etc. right away. If you
want to add more admins later, list their handles in `.env` and
restart:

```dotenv
RSSFRIENDS_ADMIN_HANDLES=you,friend
```

```bash
docker compose -f docker-compose.prod.yml up -d
```

## Step 5 · Federation health checks

Your instance is a native ActivityPub actor now. Verify from any
other machine:

```bash
# WebFinger — proves your instance can be discovered
curl -s "https://rss.mydomain.com/.well-known/webfinger?resource=acct:<your-handle>@rss.mydomain.com" | jq

# Actor document
curl -sH "Accept: application/activity+json" https://rss.mydomain.com/u/<your-handle> | jq

# NodeInfo — the metadata fediverse crawlers scrape
curl -s https://rss.mydomain.com/.well-known/nodeinfo | jq
curl -s https://rss.mydomain.com/nodeinfo/2.0 | jq
```

Every response should be a valid JSON body. If any 4xx/5xx, check
`docker compose logs app`.

To cross-verify federation, from `rssfriends.com` (or Mastodon or any
AP-speaking server) search for `@<your-handle>@rss.mydomain.com`. Your
profile should show up + be followable.

## Step 6 · Backup

The SQLite database lives at `./data/rssfriends.db`. To back up:

```bash
./scripts/backup.sh
```

This dumps a consistent snapshot into `./backups/`, keeps the newest
14, and prunes older ones. Wire it into cron:

```
0 3 * * * cd /srv/rssfriends && ./scripts/backup.sh
```

Restore with `make restore FROM=backups/rssfriends-YYYY-MM-DD.sqlite.gz`.

## Step 7 · Import from an existing reader (optional)

Once you're signed in, go to
`https://rss.mydomain.com/dashboard?panel=import` and drop your OPML
file. Any RSS reader can export OPML. Your categories + feeds
populate in seconds; the background scheduler fills items over the
next 15 minutes.

## Configuring the instance policy

A few more knobs worth knowing about, all in `.env`:

| Knob | What it does |
|---|---|
| `RSSFRIENDS_OPEN_REGISTRATION=0` | Only existing users can log in; unknown emails get an "invite-only" error |
| `RSSFRIENDS_BLOCKED_EMAIL_DOMAINS=spam.example,other.example` | These domains (and subdomains) can neither sign up nor sign in; existing accounts on them are locked out, not deleted |
| `RSSFRIENDS_ADMIN_EMAIL=you@example.com` | Shown in footer + NodeInfo metadata |
| `RSSFRIENDS_REFRESH_INTERVAL_MINUTES=15` | How often the background scheduler fetches new items |
| `RSSFRIENDS_FEED_ITEM_KEEP=100` | Max items retained per feed |
| `RSSFRIENDS_MAGIC_LINK_TTL_MINUTES=15` | Magic-link validity window |

Restart the container after any change:

```bash
docker compose -f docker-compose.prod.yml up -d
```

## Federating with rssfriends.com (or any other instance)

The follow flow works both ways:

1. **From your instance**, go to `/follow/remote` and paste
   `@farid@rssfriends.com`. Your instance sends a signed Follow;
   rssfriends.com Accepts. From then on farid's new bookmarks appear
   on your Friends timeline.
2. **From rssfriends.com** (or any AP server), someone can paste
   `@you@rss.mydomain.com` and follow you the same way.

If a follow gets stuck on "pending" for more than a minute, check
your `docker compose logs app` for HTTP-signature verification
errors or 4xx from the remote inbox.

## Updating

The GitHub repo is the source. When a new release lands:

```bash
cd /srv/rssfriends
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

The app runs a schema migration on startup. Watch the logs to
confirm the version bumped:

```bash
curl -s https://rss.mydomain.com/version
```

## Troubleshooting

**"invite-only" error for me too** — you flipped
`RSSFRIENDS_OPEN_REGISTRATION=0` before signing up. Either flip it
back to `1`, sign up, restart, then flip to `0`; or manually insert a
user row via `sqlite3 data/rssfriends.db`.

**Federation follows stay pending** — check that your `BASE_URL` uses
`https://` and matches the domain your cert is on. WebFinger + Actor
URLs are baked into signed activities, so a mismatch here means the
recipient's signature-verification fails.

**Magic-link emails go to spam** — verify your sending domain in
Resend (SPF + DKIM), and use your own domain in `RSSFRIENDS_RESEND_FROM`
rather than the default `onboarding@resend.dev`.

**Container keeps restarting** — `docker compose logs app` will show
the traceback. Common causes: bad `SESSION_SECRET` (must be quoted if
it has special chars), missing volume mount, or the SQLite file was
copied while the app was running (use `./scripts/backup.sh` instead
of raw `cp`).

## Deploying with `make deploy`

If you want the same CI-style pipeline the primary instance uses
(tests + lint + rsync + rebuild + smoke test), override the target
env vars:

```bash
make deploy \
  DEPLOY_HOST=root@your-vps \
  DEPLOY_PATH=/srv/rssfriends \
  DEPLOY_COMPOSE=docker-compose.prod.yml \
  HEALTHCHECK_URL=https://rss.mydomain.com/version
```

To avoid typing these every time, put them in `instance/deploy.mk`
(gitignored, see [`instance/README.md`](../instance/README.md)).
`make deploy` refuses to run if `DEPLOY_HOST` or `HEALTHCHECK_URL`
is missing.

This is optional — plain `git pull && docker compose up -d --build`
works fine for most instances.
