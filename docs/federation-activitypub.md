# Federation via ActivityPub — future feature

Parked for later. Key clarification: **ActivityPub is not a Mastodon
thing** — it's a W3C protocol. Once we speak it, we federate with:

- other rssFriends instances (natively — both speak AP)
- Bookwyrm (federated reading logs, the closest cousin to rssFriends)
- Mastodon, Pixelfed, Lemmy, PeerTube, WriteFreely, Misskey, GoToSocial…

So "let different rssFriends instances find each other" and "be part of
the Fediverse" are the same feature. Adopting the standard means one
implementation gets us both.

## Three viable slices

| Slice | What it does | Effort |
|-------|--------------|--------|
| **1. Discoverable** | WebFinger + Actor object + Outbox. Users on other AP servers can find you but not follow. | ~M |
| **1 + 2. Followable** | Slice 1 + signed Inbox that accepts `Follow` / `Undo` from remote actors. Real cross-instance follows work end-to-end. | ~L |
| **Full (3)** | Everything above + signed outbound delivery to every follower's inbox for every activity. Real-time Mastodon integration. | ~XL |

**Recommendation**: go directly to Slice 1+2 when we do this. Slice 1
alone is a graveyard — findable-but-not-followable feels broken. Full
federation can wait until we have real followers off-instance.

## What we'd federate

| Our concept          | ActivityPub type            |
|----------------------|-----------------------------|
| `User`               | Actor / Person              |
| `Follow` (approved)  | Follow → Accept pair        |
| `Activity.followed`  | Follow activity             |
| `Activity.favorited` | Like activity               |
| `Activity.subscribed`| Custom `radar:Subscribe`    |
| `Activity.forked`    | custom `radar:Fork`         |
| Activity log         | Outbox (AS 2.0)             |
| Feed items via `.rss`| Announce or Note w/ link    |

## Schema changes we'd need

- `User.public_key` / `User.private_key` — RSA keypair for HTTP Signatures.
  Generate on first sign-in that touches federation.
- `Follow.remote_actor` (nullable string) — URI of a remote follower.
  When set, `follower_id` may be NULL. Or: keep a shadow `User` row per
  remote actor with `visibility='remote'`. Prefer the shadow user
  approach: keeps the follow graph uniform.
- New `RemoteActor` cache: URI, inbox URL, public key, last refreshed.
- New `DeliveryQueue` table (or Redis) for outbound retries.

## Cross-instance UX

The interesting beat is that **local and remote users look identical in
the UI**, except for a suffix.

- `/discover` search accepts `@alice@radar.friend.social` — we
  WebFinger, fetch the actor JSON, cache it, render her card.
- Follow button on her card POSTs a signed `Follow` to her inbox. Her
  server sends `Accept` back. Row lands in our `Follow` table with
  `follower_id → shadow user representing @alice@remote`.
- Her stars/subscribes arrive as signed `Like`/custom `Subscribe`
  activities to our inbox. We insert them into `Activity`; they appear
  on our `/feed` timeline with a subtle `via radar.friend.social` tag.
- Cross-cherry-pick works unchanged — feeds are just URLs.
- Cross-fork same, with a "N feeds from a remote instance" prompt.

## The three parts that are painful

1. **HTTP Signatures.** Under-specified in the AP spec, Mastodon /
   GoToSocial / Misskey each interpret it slightly differently. Budget
   30-40 % of AP effort here. Read Bookwyrm's implementation first.
2. **Deliverability queue.** In-process retry is fine ≤ hundreds of
   followers; needs Redis / Celery beyond that.
3. **Trust & moderation.** Instance-level blocklist is table stakes.
   Per-user block, spam heuristics on inbox, report handling — all
   need to exist before you have real remote users.

## Bookwyrm is our reference

Bookwyrm (federated Goodreads clone) is small enough to read cover to
cover and solves the same shape of problem. Read their AP module before
we write ours.

## The decision questions we didn't resolve

1. **How many rssFriends instances do we realistically expect?**
   - 0-3 friend instances → a custom rssFriends-only JSON API is simpler
     (~M vs ~L) and skips all the AP compat pain.
   - "Be part of the Fediverse" → ActivityPub is the only choice.
2. **Is federation the product, or a feature?**
   - For Mastodon it *is* the product.
   - For Bookwyrm it's a feature that makes them 10× more interesting.
   - For rssFriends — probably feature-level.

**Decision path we've committed to**: build the app config surface (see
`docs/instance-config.md`) first so that federation can be **turned on
per instance** whenever we get to Slice 1+2. Nothing about the current
schema blocks it; we just don't build it yet.

---

*Notes from planning conversation. Adjacent doc:
[instance-config.md](instance-config.md) — the settings plumbing that
will host the eventual federation on/off + instance-blocklist knobs.*
