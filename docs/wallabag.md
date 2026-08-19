# Wallabag integration

Users can link a [Wallabag](https://wallabag.org) instance in **Settings**
to auto-archive bookmarked articles.

## Setup (per-user, one-time)

1. On your Wallabag instance, go to **Developer → Create a new client**.
   The redirect URI field can be anything (Wallabag doesn't use it for
   the password grant, which is the only OAuth flow it exposes).
2. Copy the generated **client ID** and **client secret**.
3. In FediverseRssFriends, open **Dashboard → Settings → Wallabag** and
   paste the instance URL, client credentials, and your Wallabag
   username + password.

The app exchanges those for a token pair via `grant_type=password` (the
flow used by KOReader, the mobile apps, and every other Wallabag
client — see https://doc.wallabag.org/developer/api/oauth/).

Once connected, every new bookmark POSTs the article URL to your
Wallabag in the background (best-effort — failures are logged but
don't affect the bookmark). Un-bookmarking in FediverseRssFriends does
**not** delete from Wallabag; your archive stays intact.

## Credentials at rest

The Wallabag password, client secret, and OAuth tokens are stored in
SQLite **encrypted** with Fernet (AES-128-CBC + HMAC-SHA256). The
32-byte key is derived from `RSSFRIENDS_SESSION_SECRET` via HKDF-SHA256
at runtime — no separate key file. A stolen SQLite dump (backup theft,
cloud snapshot) is opaque without the app's env; a full server
compromise (env + DB) still exposes them, since a running app must be
able to decrypt what it just encrypted. See the
`RSSFRIENDS_SESSION_SECRET` note in
[`docs/configuration.md`](configuration.md) for rotation semantics.
