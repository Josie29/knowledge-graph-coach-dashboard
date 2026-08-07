# Prod eval

What would have to be measured, watched, and alerted on to run this system for real, rather
than as a take-home. Nothing here is implemented beyond the golden tests noted below; it is
the plan a production deployment would start from.

## Metrics

**Resolver.** Precision/recall against a labeled set of coach surface forms, plus the
unresolved rate — a rising unresolved rate is a vocabulary gap while rising force-matches are
a threshold bug, so they must be tracked separately.

**Generator.** Pool-correctness golden tests on every curated-rule change (the 21/6/15
reference numbers live in CI today), plan validity rate (validator retries per request),
time-fit error, p95 latency against the ~5 s target, and token cost per plan.

**Copilot.** Citation coverage — the share of numeric claims traceable to a fetched context
slice, checkable mechanically because answers are typed and citations are a required field —
plus a hallucinated-number spot-check eval and chart-spec validity.

## Failure modes to expect

Silent resolver drift after re-embedding or an embedding model swap (the model is pinned;
re-run the boundary tests on any change). Rule gaps — hip and thoracic spine have *no*
conditions anchored to them today, so complaints there degrade to the blunt joint-level
filter (documented in [the schema doc §9](kg1-schema.md)). Stale graph after catalog edits
(the build script's integrity checks fail loudly rather than load partially). Empty pools
under tight constraints (surfaced as a 422 with the reason, never a silently degraded plan).
LLM schema misses (bounded retries; the pool-membership invariant cannot be bypassed).

## Safety monitoring

The PROV model is the monitoring hook: every exclusion is attributed to the rule engine,
never the language model — a trace showing a safety decision associated with the LLM *is a
bug visible in the data*. Alert on:

- Any plan exercise not in its pool (invariant breach — should be impossible).
- Unresolved injury constraints (the pool result carries an explicit "cannot filter — surface
  to the coach" note).
- Coach overrides of safety exclusions (recorded, attributed to the coach, and reviewed as a
  queue).

Model or prompt changes go through the golden-scenario evals before rollout.
