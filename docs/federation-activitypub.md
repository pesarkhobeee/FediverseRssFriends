# Federation via ActivityPub — enriched plan

Written against **rssFriends v0.2.1**. Where the earlier draft outlined
the shape, this version maps every existing feature onto AP and names
the exact schema, routes, and edge cases we'd need to build.

## What ActivityPub actually is

- W3C standard (2018), not tied to Mastodon.
- JSON-LD messages POSTed to per-actor **inbox** URLs, cryptographically
  signed via **HTTP Signatures**.
- Speaks the same protocol as Mastodon, Bookwyrm, Lemmy, PeerTube,
  Pixelfed, WriteFreely, Misskey, GoToSocial, and — critically — other
  rssFriends instances.

"Federated rssFriends" and "part of the Fediverse" are the same feature.

## Goals + non-goals

**In scope**
- Users on any rssFriends instance can find, follow, and be followed by
  users on other rssFriends instances or on any AP-speaking software.
- Activity events (subscribed, favorited, followed, forked) are
  discoverable across instances.
- Existing single-instance UX doesn't regress — a local-only user
  experience is unchanged.

**Explicitly not federated (yet)**
- **Feed items as AP Notes.** rssFriends is a *reader*, not a *publisher*
  of articles. The article's real publisher is the underlying RSS feed.
  Federating individual FeedItems would duplicate a graph that RSS
  already solves; skip.
- **Categories, OPML, favorites listings.** These stay local
  presentation concerns.
- **Direct messages.** Not a rssFriends feature; no reason to add them.
- **Boosts / reblogs / comments.** Same reason.

## Prior art & positioning

We are — at time of writing — filling a gap. The Venn diagram of
"self-hostable" ∩ "RSS reader" ∩ "federated" is empty.

### Fediverse-native readers
None. FreshRSS, Miniflux, Tiny Tiny RSS are all multi-user or
single-user readers with no follow / share / discovery layer. NewsBlur
has Blurblogs but it's centralized on newsblur.com, not federated.

### Federated social apps (adjacent, not readers)
- **Bookwyrm** — federated Goodreads. AP-native. Users on different
  instances follow each other and share reviews. **Same shape as
  rssFriends, applied to books.** Our closest reference architecture;
  read their signature + inbox modules before we write our own.
- **Lemmy** — federated Reddit. Community-based, same AP primitives.
- **PixelFed** — federated Instagram. Not reader-shaped, but proves AP
  works for smaller communities.
- **Mastodon** — federated Twitter. You can already follow RSS feeds
  via bridge bots (`feed2toot`, `mastofeed`), but the account you
  follow is a bot, and there's no reader UX around it.

### Federated RSS-ish experiments (the actual gap)
- Various **Mastodon-RSS bridges** — pipe an RSS feed into a Mastodon
  account. Works for reading a feed as an isolated stream; doesn't
  federate curation, categories, or reading-list sharing.
- **Fedify's RSS demo** — proof-of-concept publishing RSS items as AP
  Notes. Not used in production anywhere.
- **FEP-0ea0** (published Fediverse Enhancement Proposal) covers
  publishing feeds *into* the Fediverse, not federating readers.
- **Yarn.social** — federated Twitter-alike where each user's timeline
  IS an RSS feed. Very close in ethos ("everything is RSS") but a
  publisher, not a reader.

### Adjacent single-vendor / non-federated projects
- **Fraidycat** — spiritual ancestor. Single-user, deliberately
  non-federated. The OPML shape we import is Fraidycat's shape.
- **Feedbin** — commercial, has `feedbin.com/{user}` public pages.
  One vendor, no federation.
- **Reeder / NetNewsWire** — client-only trad RSS readers.

### Frameworks worth watching
- **Fedify** — a Python/TypeScript AP framework doing what our doc's
  "HTTP Signatures" section describes. Might be worth using instead
  of hand-rolling; evaluate at Slice 0.
- **Bonfire Social** — "federated social network kit" with pluggable
  flavors. Long-term rssFriends could position as a reader flavor of
  that stack; don't couple early.

### Positioning implications

1. **We define the terms.** Because nothing else federates
   reading-list curation, `radar:Subscribe`, `radar:Fork`, `radar:Feed`
   in this doc are the first attempt at an AP vocabulary for feed
   sharing. Worth submitting as a **FEP** once Slice 3 lands, so other
   projects can adopt or debate it. Coordinating with Bookwyrm's
   maintainers early would help; they've done this exercise once
   already for book reviews.

2. **"First mover" ≠ "big audience."** Bookwyrm is beloved by its
   users and tiny in absolute terms because most reading happens on
   Kindle / Goodreads. We should build federation for **technical
   delight and interop** — not because we expect viral growth. If
   30 people ever run an rssFriends instance and follow each other,
   that's success.

3. **Interop with Mastodon matters more than interop with other
   rssFriends instances**, at least early. Even if rssFriends stays
   the only reader in the graph, plugging into Mastodon lets us reach
   readers who already have an audience there. That's why the AP
   compatibility target in the doc is "what Mastodon actually expects"
   rather than "the AP spec to the letter."

4. **Standing on the shoulders of Bookwyrm.** Their federation module
   (~3-4k LOC of Python) is roughly what we'd build. Reading it
   cover-to-cover before Slice 0 is the highest-ROI hour of prep we
   can do.

## Two-user walkthrough

Concrete example so the design decisions later have context.

Alice is `@alice@radar.example.com` (another rssFriends instance).
Farid is `@farid@rssfriends.com` (this instance).

1. Farid opens `/discover`, types `@alice@radar.example.com` in the
   search box. rssFriends does **WebFinger** against
   `radar.example.com/.well-known/webfinger?resource=acct:alice@radar.example.com`,
   gets back the URL of her Actor object, `GET`s that JSON-LD, and
   caches a **RemoteActor** row locally.
2. Farid sees Alice's card on discover. Click *Follow* → we POST a
   signed `Follow` activity to Alice's inbox.
3. Alice's server receives the Follow. Alice is public → auto-accept.
   Her server POSTs an `Accept` back to Farid's inbox. Now:
   - Farid's local `Follow(follower=Farid, followee=@alice@radar…)` row
     is marked `approved_at=now`.
   - Alice's local `Follow(follower=@farid@rssfriends.com, followee=Alice)`
     row is marked approved.
4. Alice stars a feed item. Her server writes an Activity row and POSTs
   a signed `Like` activity to each remote inbox in her follower list
   (which now includes Farid's rssfriends.com inbox URL).
5. Farid's inbox receives the Like. Local Activity row created,
   `user_id = <alice's shadow user id>`. It appears on Farid's
   `/feed` timeline immediately.
6. On Farid's `/feed`, Alice's stars render exactly like a local user's
   would, with a small `radar.example.com` origin badge next to her
   handle.

If Alice is private, step 3 becomes "row inserted with approved_at=NULL
until Alice manually approves via her follow-requests inbox" — identical
to the local-private path we already have.

## Actor model

Every local User publishes:

```
Actor URL:      https://rssfriends.com/u/{handle}   (same URL as HTML)
Content-Type:   application/activity+json  (or +ld+json)
```

Content-negotiation on `/u/{handle}` returns HTML for browsers, JSON-LD
for AP clients (they send `Accept: application/activity+json`).

Actor JSON-LD shape (matches what Mastodon, Bookwyrm, etc. expect):

```jsonc
{
  "@context": [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    // Custom terms for radar-specific properties, see below.
    "https://rssfriends.com/ns"
  ],
  "type": "Person",
  "id":   "https://rssfriends.com/u/farid",
  "preferredUsername": "farid",
  "name": "@farid",
  "summary": "",                         // future — bio field
  "url":   "https://rssfriends.com/u/farid",
  "inbox":  "https://rssfriends.com/u/farid/inbox",
  "outbox": "https://rssfriends.com/u/farid/outbox",
  "followers": "https://rssfriends.com/u/farid/followers",
  "following": "https://rssfriends.com/u/farid/following",
  "publicKey": {
    "id":    "https://rssfriends.com/u/farid#main-key",
    "owner": "https://rssfriends.com/u/farid",
    "publicKeyPem": "-----BEGIN PUBLIC KEY-----\n..."
  },
  "manuallyApprovesFollowers": false,    // true for private users

  // Custom (radar:*) extension terms — safe to ignore for non-rssFriends
  // clients, but let other rssFriends instances render richer info.
  "radar:feedCount": 178,
  "radar:categories": ["News", "weblogs", "Github", ...],
  "radar:readingListRss": "https://rssfriends.com/u/farid.rss",
  "radar:opml": "https://rssfriends.com/u/farid.opml"
}
```

The instance itself publishes a NodeInfo document at
`/.well-known/nodeinfo` so meta-aggregators (Fediverse Observer,
fedidb.org) can list us.

## Feature-by-feature AP mapping

Everything below is grounded in v0.2.1's routes/tables so we know
exactly where the change lands.

### 1. Follow

Local User table already carries `visibility ∈ {public, unlisted,
private}` and `Follow(follower_id, followee_id, approved_at)`. To
federate:

- **Local → Remote**: on `POST /u/{handle}/follow` we detect the target
  is a URI (contains `@` after WebFinger lookup) and POST a signed
  `Follow` to its inbox. Local Follow row goes in immediately with
  `approved_at = NULL`; flips to non-NULL when we receive the `Accept`.
- **Remote → Local**: inbox receives `Follow` activity. Validate
  signature. Insert Follow row using the shadow-user id for the remote
  actor. Auto-approve if visibility ≠ private; else leave pending. Send
  a signed `Accept` (or `Reject`) back.
- **Unfollow**: `Undo` wrapping the original Follow. Delete our row.
- **Revoke follower** (existing feature): send a `Reject` activity if
  their `Accept` had been sent; remote side deletes their outbound row.
  If they later re-follow, they'll see manuallyApprovesFollowers=true
  and land in requests.

`manuallyApprovesFollowers` on the actor object is the standard flag.
Well-behaved clients will show the "pending" state; badly-behaved ones
will show us as followed prematurely — that's their problem, our
`can_view()` gate still holds.

### 2. Favorite (star)

Local `Favorite(user_id, feed_item_id)` table. Each FeedItem has a
unique id but no external URI today — need to expose one for
federation.

- **New URI**: `https://rssfriends.com/i/{item_id}` — a Note-shaped
  page that renders the item and links to the source article. Doubles
  as a landing page for social share links.
- **Local → Remote**: on `POST /items/{id}/favorite`, if the viewer has
  remote followers, broadcast a signed `Like` referencing the item URI.
- **Remote → Local**: inbox receives `Like` on one of our items.
  Insert an Activity row `kind=favorited`, `subject_url=<item URI>`,
  `user_id=<remote actor's shadow user>`. Do not persist a Favorite
  row (that's a local UX concept). Their star appears on our /feed
  timeline for their followers.
- **Unstar**: `Undo Like`. Delete the Activity row.

### 3. Subscribed (added a feed)

Custom activity — not in the core AP vocab.

```jsonc
{
  "type":  "radar:Subscribe",   // or "Add" — see open questions
  "actor": "https://rssfriends.com/u/farid",
  "object": {
    "type": "radar:Feed",
    "id":   "https://blog.rust-lang.org/feed.xml",   // canonical URL
    "name": "Rust Blog"
  },
  "target": "News"              // category name, optional
}
```

Broadcast to followers on every new `Subscription`. On the receiving
side, insert an Activity row `kind=subscribed`.

Non-rssFriends clients will see this as an unknown type — Mastodon
tends to ignore custom types silently, which is fine.

### 4. Forked (cloned a whole radar)

```jsonc
{
  "type": "radar:Fork",
  "actor": "https://rssfriends.com/u/farid",
  "object": "https://radar.example.com/u/alice",     // the forked actor
  "summary": "Forked @alice — 12 feeds added"
}
```

Only broadcast once, aggregated. Same rendering.

### 5. Followed (someone)

Beyond the `Follow` protocol activity itself, our Activity log records
`kind=followed` when a local user follows anyone. To federate this
into the outbox, emit a standalone entry so it appears in the outbox
collection alongside the protocol activity.

### 6. Created category

**Not federated.** Category is a local presentation concept; no reason
to broadcast. Skip.

### 7. Reading list RSS (`/u/{handle}.rss`)

Already an RSS feed. Continues to work unchanged. Exposed on the actor
as `radar:readingListRss`, which lets other rssFriends instances offer
a "Subscribe to their RSS in your reader" button.

### 8. Activity RSS (`/u/{handle}/activity.rss`)

Same — remains RSS. Exposed on the actor. AP-savvy clients will use
the actor's `outbox` instead, but both work.

### 9. Discover

Search extends beyond the local instance:

- If the query looks like an `@user@host` string, WebFinger it first,
  cache the actor, show them at the top of the results.
- Local fuzzy search continues alongside.

`/discover/popular` starts including a "known remote feeds" section
once we're receiving `radar:Subscribe` activities from federated
users.

## Data schema additions

**Six new tables**, all small.

```sql
-- Actor cache. Every remote user we've heard about lives here.
CREATE TABLE remote_actor (
  id              INTEGER PRIMARY KEY,
  actor_uri       TEXT NOT NULL UNIQUE,        -- https://radar.example/u/alice
  handle          TEXT NOT NULL,               -- "alice"
  host            TEXT NOT NULL,               -- "radar.example.com"
  inbox_uri       TEXT NOT NULL,
  shared_inbox    TEXT,                        -- optional bulk endpoint
  public_key_pem  TEXT NOT NULL,
  display_name    TEXT DEFAULT '',
  icon_url        TEXT DEFAULT '',
  last_refreshed  DATETIME NOT NULL
);

-- A shadow User row exists for every remote actor we interact with,
-- so the follow/favorite tables can keep using integer user_ids.
-- User table grows one column:
ALTER TABLE user ADD COLUMN remote_actor_id INTEGER NULL
  REFERENCES remote_actor(id);
-- When remote_actor_id IS NOT NULL, that User row is a shadow of a
-- remote person. The Follow/Favorite/Activity tables keep working
-- unchanged.

-- Every LOCAL user gets an RSA keypair for HTTP Signatures.
ALTER TABLE user ADD COLUMN public_key_pem  TEXT NULL;
ALTER TABLE user ADD COLUMN private_key_pem TEXT NULL;   -- encrypted at rest? see open question

-- Outbound delivery queue. Retries on network failure.
CREATE TABLE ap_delivery (
  id            INTEGER PRIMARY KEY,
  target_inbox  TEXT NOT NULL,
  actor_id      INTEGER NOT NULL REFERENCES user(id),
  activity_json TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_attempt  DATETIME NOT NULL,
  dead_at       DATETIME NULL,                 -- gave up
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ap_delivery_pending_idx
  ON ap_delivery(next_attempt) WHERE dead_at IS NULL;

-- Inbound deduplication (activities are re-sent freely; ignore repeats).
CREATE TABLE ap_seen (
  activity_id  TEXT PRIMARY KEY,               -- the "id" URI of the activity
  received_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Per-instance blocklist + allowlist (managed via /admin — the config
-- surface planned in docs/instance-config.md).
CREATE TABLE instance_policy (
  host       TEXT PRIMARY KEY,
  mode       TEXT NOT NULL,                    -- 'block' | 'allow'
  reason     TEXT DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Rationale for the shadow-user approach: **every existing route and
table keeps working**. `Follow(follower_id, followee_id)`, favorites,
activity — all keep using integer user ids. No massive refactor of the
five biggest queries in the app. The trade-off is a small
`is_remote()` helper wherever we render `@handle` (append `@host` if
remote).

## Route additions

Sixteen new routes total. Grouped by phase.

**Discoverability (Slice 0)**
- `GET  /.well-known/webfinger?resource=acct:{handle}@rssfriends.com`
- `GET  /.well-known/nodeinfo` + `GET /nodeinfo/2.1`
- `GET  /u/{handle}` (existing — content-negotiate to JSON-LD)
- `GET  /u/{handle}/outbox` — paginated AS 2.0 collection of activities
- `GET  /u/{handle}/followers` (existing — content-negotiate)
- `GET  /u/{handle}/following` (existing — content-negotiate)

**Followable (Slice 1)**
- `POST /u/{handle}/inbox` — signed activity receiver
- `POST /inbox` — shared instance-wide inbox (optional; efficient
  broadcast target for shared_inbox-aware senders)
- `GET  /i/{item_id}` — Note-shaped landing page for a feed item,
  content-negotiate to JSON-LD
- Discover search accepts `@user@host` and does WebFinger

**Full federation (Slice 3)**
- (Existing routes internally start emitting to `ap_delivery`. No new
  URL paths; the change is all outbound-side glue.)

## HTTP Signatures — the painful part

The AP spec references
[draft-cavage-http-signatures](https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures-12).
Under-specified, and every implementation has quirks.

**Signing (outbound)**
1. Compute a SHA-256 digest of the JSON body →
   `Digest: SHA-256=<base64>`
2. Sign a canonical string built from `(request-target)`, `host`,
   `date`, `digest` headers with the local user's RSA private key
   (PKCS#1 v1.5, SHA-256).
3. Attach the base64 signature + key id in the `Signature:` header.

**Verifying (inbound)**
1. Parse the `Signature:` header (comma-separated k="v" pairs; quotes
   NOT always doubled; whitespace varies by client — write a robust
   parser).
2. Fetch the actor's public key (cached in `remote_actor`; refresh if
   the `keyId` fragment differs from cache).
3. Rebuild the canonical string using the exact set of headers the
   signer named (order matters).
4. Verify RSA + PKCS#1 v1.5 + SHA-256.
5. Verify the `Digest:` header matches SHA-256 of the actual body.
6. Reject if `Date:` header is more than 12h off from server time
   (replay protection; standard practice).

Library options:
- **cryptography** (already a transitive dep) for RSA
- **PyLD** or hand-roll for JSON-LD normalization (probably hand-roll —
  we control both ends of our JSON shape, JSON-LD's canonicalization
  algorithm is way too much surface area to trust)

**Read Bookwyrm's `bookwyrm/activitypub/base_activity.py` and their
signature module before starting.** That's a small, opinionated,
production-tested implementation of the same subset we need.

## Delivery model

In-process background thread pool + retry queue.

- On any activity emission (favorite, follow, subscribe, fork), enqueue
  one row per remote inbox that follows the actor.
- Worker picks up rows where `next_attempt <= now() AND dead_at IS
  NULL`, signs + POSTs.
- Response codes:
  - `2xx` — delete row.
  - `4xx` (except 429) — delete row and log; don't retry client errors.
  - `429` or `5xx` or network fail — bump `attempts`,
    `next_attempt = now() + backoff(attempts)`. Cap at 7 attempts
    (~24h with exponential backoff), then set `dead_at`.
- Dedup: use `shared_inbox` for a followee's followers when many share
  the same host (standard AP optimization).

Runs in-process using the same `ThreadPoolExecutor` shape as our
refresh cycle. Fine to a few hundred followers per user; if we outgrow
that we swap in Redis + Celery under the same worker function.

## Inbox validation + safety

Every inbox POST goes through, in order:

1. **Instance-level policy.** If the sender's host is on our blocklist,
   403 immediately. If we're in allowlist mode and host isn't listed,
   same.
2. **Signature verification** (see above).
3. **Replay dedup.** Reject if `activity.id` already in `ap_seen`.
4. **Actor freshness.** If cached actor is > 24h old, refetch before
   trusting it.
5. **Rate limit** per sender host. Slot in the existing HTMX
   rate-limit infra.
6. Deserialize + handle. Any of `Follow`, `Undo`, `Accept`, `Reject`,
   `Like`, `Undo Like`, `Delete Actor`, `radar:Subscribe`,
   `radar:Fork`. Ignore everything else.

## Slice roadmap — revised

Bigger and more concrete than the earlier draft.

### Slice 0 (2–3 PRs, mostly wiring): key material + actor JSON

- Add columns: `public_key_pem`, `private_key_pem` on `user`. Generate
  on first login (or in a one-shot migration for existing users).
- Content-negotiate `/u/{handle}` → return the actor JSON-LD when
  `Accept` matches AP MIME types.
- WebFinger + NodeInfo. Both are pure-lookup and no writes.
- No follow/like federation yet.

**What this ships**: rssFriends instances become *discoverable* by any
AP client. A Mastodon user searching `@farid@rssfriends.com` gets a
card, can click through — but can't follow yet. That's fine as a
milestone because it lets us test key generation, JSON-LD shape, and
WebFinger against real Mastodon in the wild before adding writes.

### Slice 1 (5–7 PRs): inbound follow

- `POST /u/{handle}/inbox` accepting only `Follow` and `Undo`.
- HTTP Signatures verify path — the big one.
- Actor fetch + cache.
- Send back `Accept` / `Reject` — this requires the outbound signer
  and one-shot delivery (no queue yet — best-effort direct POST).
- Local UI: remote followers appear in `/u/{handle}/followers` with
  their `@handle@host` displayed. In the follow-requests inbox for
  private users. Approve/reject buttons trigger the AP Accept/Reject.

**What this ships**: a Mastodon user can follow you and it actually
works. Their approval-required follow lands in your requests inbox.

### Slice 2 (4–5 PRs): outbound follow + delivery queue

- Follow button on discover / on `/u/{handle}` for a remote actor.
- `ap_delivery` table + worker.
- Retry with backoff.
- Send `Follow` to remote inbox; wait for `Accept`.

**What this ships**: you can follow anyone on the Fediverse from your
rssFriends account. Their public activity starts arriving in your
`/feed` timeline.

### Slice 3 (4–5 PRs): outbound activities

- On favorite/subscribe/fork, enqueue outbound `Like` /
  `radar:Subscribe` / `radar:Fork` to every approved remote follower.
- Inbound handling of the same types, dropping them into our Activity
  table.
- `/i/{item_id}` Note landing page for `Like` targets.

**What this ships**: real-time bidirectional participation. Your stars
show up in Mastodon timelines. Their `Like`s show up on your `/feed`.

### Slice 4 (3–4 PRs): moderation

- Instance blocklist admin page under `/admin` (from
  `docs/instance-config.md`).
- Per-user block.
- Report activity type → moderation queue.
- Rate limits per remote host.

## Config surface

All of these are Tier-2 settings in `docs/instance-config.md`, editable
by an admin from the `/admin` UI:

```
federation.enabled                bool     default off
federation.mode                   enum     'off' | 'allowlist' | 'open'
federation.allowlist              json     [] of host strings
federation.blocklist              json     [] of host strings
federation.remote_follow_default  enum     'auto_approve' | 'pending'
federation.max_delivery_attempts  int      default 7
federation.actor_refresh_hours    int      default 24
federation.rate_limit_per_host    int      per-minute cap on inbox POSTs
```

Instance-config PR 1 (from that doc) is a prerequisite for turning
federation on/off per instance.

## Testing strategy

- **Unit** — HTTP Signature sign + verify roundtrip; canonical string
  construction; digest generation; WebFinger + actor JSON shape.
- **Integration** — spin up two instances via Docker Compose, wire
  their hostnames via /etc/hosts, run a fixture-based test that
  federates a Follow, an Accept, a Like, an Undo. Snapshot the
  resulting DB state on both sides.
- **Compatibility** — a small script that talks to a real Mastodon
  test server. Verify our actor JSON, WebFinger, follow flow round-
  trip against `mastodon.social` staging or a self-hosted GoToSocial.

## Migration risks

1. **Handle collisions.** Our current UI shows `@farid` everywhere.
   Once federated, `@farid` on rssfriends.com and `@farid@other.instance`
   are different people. Every render site needs a small helper —
   `render_handle(user)` — that appends `@host` when the user is
   remote. Trivial but touches many templates.
2. **Public radars become AP-discoverable.** They're already
   `visibility=public` and google-indexable, so this doesn't leak
   anything new — but the surface area grows. Confirm intent before
   flipping `federation.enabled=true`.
3. **RSA key at rest.** The private key is powerful. Options:
   - Store plaintext (like most Mastodon-descendants do — the DB is
     already sensitive).
   - Encrypt with `SESSION_SECRET`-derived key.
   - Use a separate config-managed key.
   Recommend option 2 for cheap defence-in-depth.
4. **Old followers list URL is not the same as AP followers**. The
   existing `/u/{handle}/followers` HTML page must continue to work
   for browser visitors; only content-negotiated JSON-LD access
   returns the AP Collection shape.
5. **Item URIs are permanent.** Once we start emitting `/i/{item_id}`
   as Note ids, we can never renumber. Add a stable public identifier
   column (`FeedItem.public_id = hex(secrets)`) so item table
   restructuring doesn't break external references.

## Bookwyrm as reference

Bookwyrm is our closest cousin (federated Goodreads clone) and is
small enough to read cover-to-cover. Before writing our first line of
signature code, read these files:

- `bookwyrm/models/user.py` — key generation
- `bookwyrm/activitypub/verify.py` — inbound signature verification
- `bookwyrm/broadcast.py` — outbound queue
- `bookwyrm/views/inbox.py` — the dispatcher
- `bookwyrm/models/relationship.py` — follow / block state machine

Time investment: 2–3 hours. Saves probably a week of debugging.

## Open questions worth resolving before Slice 0

1. **Are we ok with a JSON-LD "extension" namespace?**
   `radar:Subscribe`, `radar:Feed`, etc. are custom activity/type
   names. Standard AP practice, but means non-rssFriends clients treat
   them as opaque. That's what we want — but worth confirming.
2. **Shadow-user vs separate table for remote actors?** This doc
   argues for shadow-user (drop-in compatible with existing tables).
   Alternative: dedicated `RemoteFollow` / `RemoteFavorite` tables and
   a union view at read time. The shadow approach is simpler; commit
   to it.
3. **Single inbox at `/inbox` in addition to per-user
   `/u/{handle}/inbox`?** Standard optimization; small extra work.
   Recommend yes.
4. **How much of the AP JSON-LD do we actually parse?** Full JSON-LD
   canonicalization is enormous. Pragmatic answer: treat the JSON as
   plain JSON, key off the `type` and `@context` fields, ignore
   anything we don't recognize. Mastodon does approximately this.
5. **What triggers a first-time key generation for existing users?**
   Options: lazy on first federation-related request; eager one-shot
   migration; never (only new users get keys). Recommend one-shot
   migration to avoid subtle "some users can't be followed" bugs.

## Estimate — end-to-end

| Slice | LOC (rough)  | Effort  | Ship-value |
|-------|--------------|---------|------------|
| 0     | ~600 + tests | ~M      | Discoverable — search from Mastodon |
| 1     | ~900 + tests | ~L      | Followable — Mastodon can follow you |
| 2     | ~700 + tests | ~M      | You can follow anyone in the Fediverse |
| 3     | ~800 + tests | ~M-L    | Live cross-instance activity |
| 4     | ~500 + tests | ~M      | Moderation / instance policy |

Total: **~3500 LOC + tests**. Comparable to Bookwyrm's federation
module size, which they built in roughly 6 months of part-time work.
Suggests **1–3 months** for us to hit Slice 2 (usable federation),
another 1–2 for Slices 3 + 4.

Prerequisites before we touch a line of AP code: **admin/config
plumbing must land** (`docs/instance-config.md` PR 1), so federation
can be toggled per instance without redeploy.

---

*Adjacent doc: [instance-config.md](instance-config.md) — the settings
plumbing that hosts the federation switches above.*
