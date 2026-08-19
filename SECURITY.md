# Security policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-sensitive
reports. Instead, either:

- Open a [private security advisory](https://github.com/pesarkhobeee/FediverseRssFriends/security/advisories/new) on this repository, or
- Email `security@rssfriends.com` if you can't use GitHub.

I aim to acknowledge reports within 48 hours and to ship a fix (or a
public workaround) within 14 days for anything that could compromise
another instance's data.

## Scope

rssFriends is one Python + FastAPI process talking to SQLite. What I
consider in-scope for a security report:

- Anything that leaks a user's private data (bookmarks, email,
  session cookie, ActivityPub private key)
- Anything that lets a caller impersonate another user (session
  hijack, HTTP-Signature bypass on the AP inbox, WebFinger spoof)
- Anything that lets one instance corrupt another's data via
  federation (Follow / Accept / Delete spoofing)
- Local-file / server-side-request-forgery via feed URLs
- Denial-of-service that's practical against a small VPS (a single
  bad payload that stalls the event loop, a memory bomb via feed
  content, etc.)

Not in scope:

- Missing rate limits on public endpoints (documented tradeoff — see
  `docs/self-host.md` § "Configuring the instance policy")
- Missing CAPTCHAs / bot filtering on signup (magic-link auth wall
  filters most attempts; use `RSSFRIENDS_OPEN_REGISTRATION=0` for
  higher-risk contexts)
- Anything that requires a compromised admin session already

## Key handling

Two secrets matter, both env-var-driven:

- **`RSSFRIENDS_SESSION_SECRET`** — signs session cookies, derives
  the Fernet key that encrypts stored Wallabag credentials, mixes
  into `activity.rss` signed URLs. Rotating invalidates all three at
  once (intentional). See `app/crypto.py`.
- **Per-user ActivityPub keypair** — RSA 2048, PKCS#8 PEM. Stored
  plaintext in the SQLite `user` table (`ap_private_key`
  column). Same posture as `User.activity_secret` and Wallabag tokens
  (the whole DB file is your trust boundary). Consider full-disk
  encryption on the VPS if that matters for your threat model.

## What's known-not-hardened

Listed so you can decide if it matters for your instance:

- Delivery-queue retries: outbound Note deliveries are fire-and-forget
  daemon threads. A single hachyderm outage means those Notes are
  lost from that follower's timeline.
- Inbox rate limiting: no per-actor throttle. A malicious peer could
  submit thousands of Follows in a burst.
- HTTP-Signature draft-cavage only. RFC 9421 (the new spec) isn't
  supported yet — Mastodon currently uses the draft, so we're
  compatible with the fediverse but not the RFC.
