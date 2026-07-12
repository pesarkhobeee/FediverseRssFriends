# Instance configuration — plan

Goal: an instance manager (the person who runs the box) can adjust
per-instance behavior from a web UI without shell access, and every
future feature (including federation) plugs into the same settings
surface.

## Storage — hybrid, on purpose

Two tiers of settings.

**Tier 1 — bootstrap** (env vars, chicken-and-egg for booting the app):
- `RSSRADAR_SESSION_SECRET`
- `RSSRADAR_DATABASE_URL`
- `RSSRADAR_BASE_URL`
- `RSSRADAR_ENABLE_SCHEDULER`
- `RSSRADAR_SMTP_HOST` / `_USER` / `_PASSWORD` / etc.

These are set in `.env` / systemd / compose. Changing them requires a
restart. Leave them where they are.

**Tier 2 — runtime** (new `InstanceSetting` table, editable via admin UI):
- Instance identity: name, description, contact email, logo URL
- Signup policy: `open` / `invite_only` / `closed`, allowed email
  domains, default visibility for new users
- Feature toggles: fork, cherry_pick, OPML import, public discover,
  reading RSS, activity RSS
- Refresh: interval, batch size, concurrency, per-user feed cap,
  per-feed item retention
- Content policy: max OPML upload MB, max item summary chars
- Federation (parked): `federation_enabled`, `instance_allowlist`,
  `instance_blocklist`
- Presentation: theme color, custom footer HTML

Rule: **any setting a manager might want to change on a running system
lives in Tier 2**. Anything you'd only touch during deploy stays Tier 1.

## Schema

Single generic key-value table:

```
InstanceSetting:
  key: str primary_key           # 'instance.name', 'signup.mode', ...
  value: str                     # JSON-encoded scalar or array
  updated_at: datetime
  updated_by: int | None         # foreign key to user.id
```

Rationale: JSON-in-VARCHAR is fine at instance-config scale (few dozen
rows, read-heavy, cache in memory). Beats a wide typed table because
adding a setting is one INSERT, not a migration.

Every read goes through `settings.get(key, default)` which:
1. Checks in-memory cache (fresh for 60s).
2. Falls back to DB.
3. Falls back to hardcoded defaults.

This means old code paths keep working — every current env-driven
config value becomes the *default* for its matching setting key.

## Admin role

Add `User.is_admin: bool`. Populated by:

- `RSSRADAR_ADMIN_EMAILS=a@ex.com,b@ex.com` env var — matched on login
  and applied idempotently.
- Or first user on a fresh DB → auto-admin (bootstrap).
- Or granted via existing admin's UI.

Everything under `/admin/*` requires `is_admin=True`. Non-admins get
404 (not 403) so admin routes aren't discoverable.

## Signup gating

Uses `signup.mode`:

- **open** (current default): any valid email can sign in for the first
  time and get a fresh account.
- **invite_only**: `/login` still works for existing users; unknown
  emails need to present an invite code in the URL (`?invite=X`).
  Admin UI mints codes; each code is single-use.
- **closed**: no new signups; only existing users log in.

Optional `signup.email_domains`: comma-separated whitelist. Applied
regardless of mode.

## Admin UI — three pages

1. **`/admin`** — settings dashboard. One form per section (identity,
   signup, features, refresh, content policy). Save button per section.
   Uses the same HTMX pattern as the rest of the app.
2. **`/admin/users`** — list of users with columns (handle, email,
   visibility, is_admin, created_at, feed count). Actions: toggle
   admin, disable account, delete account (asks twice).
3. **`/admin/invites`** — mint, list, revoke invite codes. Only used
   when `signup.mode == 'invite_only'`.

Skip for MVP: reports, moderation queue, instance blocklist UI.
Federation settings render as `disabled + coming soon` placeholder now
so we lock in the layout.

## Rollout — three PRs

**PR 1 — plumbing (Tier 2 + admin role)**
- `InstanceSetting` table + migration
- `app/settings.py` — `get()`, `set()`, cache, defaults registry
- `User.is_admin` column + migration + `is_admin` dependency
- `RSSRADAR_ADMIN_EMAILS` env var applied on login
- No UI yet; everything else in the app still uses defaults exactly as
  it does today, so this is a no-op change functionally

**PR 2 — admin UI (settings dashboard)**
- `/admin` route + template
- Wires the existing Tier 1 defaults through `settings.get()` so the
  UI actually influences behavior. Refactor 4-5 read sites.
- Instance name / description now appear in `base.html` header (via
  a Jinja global that reads `settings.get('instance.name')`)

**PR 3 — signup gating + invites + user admin**
- `signup.mode` respected in `/login`
- `Invite` table + `/admin/invites` page
- `/admin/users` page + admin actions

Each PR ships useful behavior on its own. If we stop after PR 1 we've
still added `is_admin` correctly; after PR 2 the manager can rename the
instance; after PR 3 the whole thing is a real hosted product.

## Federation-forward design

The whole reason we're doing this now is so that when we ship
federation later, the switches already exist:

- `federation.enabled` (bool)
- `federation.mode` — `off` / `allowlist` / `open`
- `federation.allowlist` (JSON array of hostnames)
- `federation.blocklist` (JSON array of hostnames)
- `federation.remote_follow_default` — `auto_approve` / `pending`

These are just more keys in the same `InstanceSetting` table. The
admin UI shows them under a "Federation" section that's collapsed +
disabled until AP support ships. Zero migration needed to activate.

## What NOT to store this way

- User-scoped preferences (theme, sort order): those belong on `User`
  or a `UserSetting` table. Don't confuse per-user with per-instance.
- Secrets (SMTP password, session secret): stay in env, never in DB.
  Admin UI never displays or edits these.
- Rate-limits when they get sophisticated: eventually a separate
  Redis-shaped store; not our concern now.

## Estimate

- PR 1: ~S. Table + settings module + admin gate + tests.
- PR 2: ~M. Admin dashboard is the biggest piece. ~4-5 read-site
  refactors to route through `settings.get()`.
- PR 3: ~M. Invite flow is fiddly; user list is easy.

Total: ~L for the full three, but each PR is ~S–M and independently
useful.

---

*Adjacent doc:
[federation-activitypub.md](federation-activitypub.md) — the parked
federation plan that will use the settings surface we build here.*
