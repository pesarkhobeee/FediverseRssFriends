# Attribution chain — future feature

Reframes rssRadar from "a reader" to **a public credit graph for
curation**. For every subscription, remember *where it came from* — then
render both the inbound genealogy ("who I learned from") and outbound
reach ("who took feeds from me").

Nothing else does this for RSS, and it makes the "Most-forked curators"
signal on `/discover` meaningful instead of gameable.

## Data model — small

Two nullable columns on `Subscription`:

- `source_kind`   : `'manual' | 'cherry_pick' | 'fork' | 'opml'`
- `source_user_id`: `int | None` (only set for `cherry_pick` and `fork`)

Migration same shape as `activity_secret` — ALTER TABLE ADD COLUMN, backfill
all existing rows as `manual`.

**One-hop only.** If Alice adds feed X, Bob cherry-picks, Carol picks from
Bob → Carol's row credits Bob. Honest ("Bob introduced me") without the
complexity of a full chain.

## Write sites to instrument

Four call sites, all already exist:

| Call site                     | source_kind     | source_user_id |
|-------------------------------|-----------------|----------------|
| `dashboard.subscribe`         | `manual`        | `None`         |
| `public.cherry_pick`          | `cherry_pick`   | owner.id       |
| `importers.fork_from`         | `fork`          | owner.id       |
| `dashboard.import_opml`       | `opml`          | `None`         |

`importers.add_subscription_by_feed` grows two optional kwargs; existing
callers pass them.

## Read queries — indexed group-bys

**Inbound: "who gave me feeds":**

```sql
SELECT source_user_id, COUNT(DISTINCT feed_id)
FROM subscription
WHERE user_id = :me AND source_user_id IS NOT NULL
GROUP BY source_user_id;
```

**Outbound: "who took from me" — count DISTINCT downstream users** (not
events) so someone can't inflate their number by mass-cherry-picking from
one friend:

```sql
SELECT COUNT(DISTINCT user_id)
FROM subscription
WHERE source_user_id = :me AND user_id != :me;
```

Both hit the same `(source_user_id)` index.

## UI surfaces (in priority order)

1. **Dashboard stripe** under the radar title:
   > **Your radar: 47 feeds** · 32 you added yourself · 12 from @alice ·
   > 3 cherry-picked from @bob, @carol

2. **`/u/{handle}/credits`** page — two columns:
   > **You got from others**
   > · 12 from @alice · 4 from @bob
   >
   > **Your reach**
   > · 8 distinct people have taken from you
   > · most-copied feed: "Rustacean" (5 people)

3. **`/discover` — Most-forked curators** query changes from counting
   `Activity` rows to counting `COUNT(DISTINCT user_id)` on Subscription
   where `source_user_id = user.id`. Much better signal.

Skip the per-row "from @alice" chip on dashboard rows — visual noise.

## Privacy

If the source user is `visibility='private'`:
- Owner's dashboard: still show the name (they know their own history).
- Public page + `/u/{handle}/credits` for anyone else: show
  "cherry-picked" without a name. Don't leak private handles via the
  attribution graph.

## Estimate

~M, roughly the size of step 8 (follow):
- migration: 5 LOC
- two column reads + writes across four sites: ~50 LOC
- one dashboard partial: ~30 LOC
- one `/credits` page + route + template: ~80 LOC
- tests: ~200 LOC
- discover query update: 3 LOC

Total ~370 LOC + tests.

## Deliberately out of scope

- Full chain (multi-hop) — quadratic query cost, marginal user value.
- Backfilling attribution from existing `Activity` rows — separate script;
  new data starts clean.
- Public attribution of items (who introduced whom to a specific *post*) —
  we track feed-level, not item-level.

---

*Notes from planning conversation. Not scheduled yet — pick up after
ActivityPub if it still feels worthwhile.*
