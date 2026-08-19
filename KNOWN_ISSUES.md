# Known issues (legacy 0.9.x)

This is the legacy Python app that lives on the `poc` branch. Active
development has moved to the AP-native rewrite tracked in
[Milestones on GitHub](https://github.com/pesarkhobeee/FediverseRssFriends/milestones).
Small legacy bugs are noted here rather than filed as issues, because
the rewrite will re-implement the affected features from scratch and
those decisions are being made per-milestone.

## Data drift: renaming a category doesn't cascade to auto-created category-follow feeds

**Discovered**: 2026-08-02 while investigating @julian-lamas' "removed
feed keeps popping back" report.

**Symptom**: when a user (call them A) follows a category owned by user B,
`follow.py::_ensure_category_subscription` creates a `Feed` row with
`url = ".../u/{B.handle}/c/{category_name}.rss"`. If B later renames
that category, the Feed row's URL is not rewritten — it still bakes in
the old name.

**Concrete impact**: 
- The 0.9.5 fix that drops the linked `CategoryFollow` when unsubscribing
  from a category-follow feed uses URL-based lookup (parse `{owner}`
  and `{cat_name}` out of the URL, then look up the Category by name).
  If the category has been renamed since the follow, that lookup fails
  and the CategoryFollow row is orphaned. User's manual unsubscribe still
  works (Subscription deleted), but the graph edge stays.
- Fetch cycles keep working because the server's per-category RSS
  route matches category names case-insensitively — so a request for
  `/c/old-name.rss` still resolves to the current row if the case only
  changed, but breaks if the name changed materially.

**Fix shape** (when addressed):
Mirror the handle-rename story in `app/public.py::rename_handle`:
inside the transaction, iterate every `Feed` row whose URL contains
`/u/{owner_handle}/c/{old_category_name}.rss` and rewrite to the new
name. Small: ~20 lines + one test.

Not a blocker because:
- Category renames are rare in practice.
- `_ensure_category_subscription` is one of few places creating URL-baked
  Feed rows — no other code path drifts this way.
- The AP-native rewrite (Milestone 2, issue #14 · Category + Subscription
  models) will design this from the start.

**Related**: `app/dashboard.py::rename_category` has an inline TODO
pointing here.
