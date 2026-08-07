# Churn risk — how the level is computed

The coach dashboard shows every member a churn-risk level. This document is the whole method:
the four signals, the points each is worth, where the band cut-points sit, and what the number
deliberately is not.

The implementation is [`backend/app/kg/churn.py`](../backend/app/kg/churn.py); it runs at graph
build time and its output lands on a `:ChurnAssessment` node. Every threshold quoted below is a
named constant in that module.

> **Headline.** The dataset ships a hand-written churn level, and one of its three stated reasons
> ("login frequency down vs. prior month") has no backing field anywhere in the data. We do not
> ingest it. The level is computed instead, from adherence and workout history only, so that every
> reason shown to a coach names a number the coach can go and check. See
> [§6](#6-what-this-is-not) for what that costs.

---

## 1. Why this exists

`data/member-context.json` contains a `coach_brief.churn_risk` block with a level and three reason
strings. Before this change, `build_kg.py` copied it verbatim onto the `CoachBrief` node and the UI
rendered it. Nothing tied it to the member's behaviour: had the file said 100% adherence and
"elevated", the dashboard would have said elevated.

Two problems, and the second is the serious one:

1. **It cannot be recomputed.** A level that arrives as a literal cannot respond to new data. Next
   week's adherence changes nothing.
2. **One reason was unfalsifiable.** Login and app-session frequency are not recorded anywhere in
   the dataset (catalogued as quirk 11 in [`data-overview.md`](./data-overview.md)). The copilot's
   only possible citation for that claim was the brief asserting it — which is a claim citing
   itself. The previous mitigation was an instruction telling the agent to attribute it to the
   brief, plus an italic caveat in the UI.

Computing the level from real fields dissolves the second problem rather than mitigating it: there
is no longer anything in the graph for a model to cite. The coach brief keeps its morning tasks and
its `generated_for` date (which is also the dataset's `now_anchor`); only the churn block is dropped.

---

## 2. The four signals

Each signal asks one question, and awards points according to how bad the answer is. A signal that
does not fire contributes nothing and produces no reason string.

| # | Signal | Question | Source | Thresholds → points | Max |
|---|---|---|---|---|--:|
| 1 | `adherence_drop` | Are they completing less than they used to? | `adherence.weekly_completion_pct` | ≥20pp → 3 · ≥10pp → 2 · ≥5pp → 1 | 3 |
| 2 | `adherence_floor` | Is the current week low in absolute terms? | most recent `pct` | <60% → 2 · <80% → 1 | 2 |
| 3 | `skipped_sessions` | Did they plan sessions and not do them? | `workout_history` where `planned ∧ ¬completed` | ≥2 → 2 · 1 → 1 | 2 |
| 4 | `workout_silence` | How long since they last finished anything? | latest `completed` date | ≥15d → 3 · ≥11d → 2 · ≥8d → 1 | 3 |

**Maximum possible score: 10.**

### Why two adherence signals rather than one

They catch opposite failures. `adherence_drop` compares the mean of the last two weeks against the
mean of the two before it, so it sees a member falling away from a good baseline — but it is blind
to someone who has been at 40% since the day they joined, because that member has no trend. The
`adherence_floor` check is what catches them. Conversely a member sliding from 100% to 80% trips the
trend signal while sitting comfortably above any floor.

The 2-vs-2 mean is deliberate: a week-over-week difference would treat one holiday week as a crisis.
It also means the signal needs four weeks of history and stays silent below that, rather than
comparing uneven windows and inventing a trend for a new member.

### Why `planned` matters for skipped sessions

Signal 3 only counts sessions the member scheduled and then did not complete — `planned ∧
¬completed`. A day with no session on the calendar is a rest day. Counting absence of a workout as a
skip would flag every member on earth.

### Everything is measured against `now_anchor`, never the wall clock

The dataset lives in mid-2026 and its "today" is the coach brief's `generated_for` date
(`2026-06-04`), stored as `Member.now_anchor` — quirk 13. Signals 3 and 4 do date arithmetic, and
both take the anchor as their reference. Scoring against the real current date would make the demo
member look more abandoned every day the repository sits unopened.

---

## 3. Bands

| Score | Level |
|---|---|
| 0–1 | `low` |
| 2–4 | `moderate` |
| 5–10 | `elevated` |

The cut-points are not arbitrary. The largest any single signal can score is 3, and the `elevated`
floor is 5 — so **no one signal can reach `elevated` on its own**. An elevated member always has at
least two independent things going wrong. That property is what the bands were chosen to produce,
and a test asserts it directly against the threshold tables so that retuning a signal past the band
floor fails the build instead of quietly changing what coaches are told.

The `moderate` floor of 2 is looser on purpose: a single mid-strength signal is worth a coach
glancing at, and `moderate` is a prompt to look, not an alarm.

---

## 4. Worked example — the sample member

Jordan Rivera, anchored at `2026-06-04`. Adherence `100, 100, 75, 50`; four workouts, one of them
planned-but-not-completed on `2026-05-29`; last completed session `2026-06-03`.

| Signal | Computation | Points |
|---|---|--:|
| `adherence_drop` | (75+50)/2 = 62.5 vs (100+100)/2 = 100 → 37.5pp fall | **3** |
| `adherence_floor` | latest week 50%, under the 60% floor | **2** |
| `skipped_sessions` | 1 skip in the 28 days to 2026-06-04 | **1** |
| `workout_silence` | 1 day since 2026-06-03 — did not fire | 0 |
| | **6 of 10 → `elevated`** | **6** |

The three reasons stored on the node:

```
Adherence fell 100% -> 62.5% (2-week average vs the prior 2 weeks), a 37.5 point drop
Week of 2026-06-02 finished at 50%, below the 60% floor
1 planned session skipped in the 28 days to 2026-06-04 (2026-05-29)
```

This lands on the same level the coach hand-wrote, which is a mild reassurance that the thresholds
are not absurd — but note what changed. The shipped version offered two checkable reasons and one
that could not be checked at all. This version offers three, each naming a figure that exists in the
member's record. `workout_silence` staying silent is also informative: Jordan is disengaging from
her *plan*, not from training altogether, and the reason list says so by omission.

---

## 5. Where it is computed, and why there

Build time, materialised onto a node:

```
(:Member)-[:HAS_CHURN_ASSESSMENT]->(:ChurnAssessment:MemberFact)
    id           "churn_2026-06-04"
    level        "elevated"
    score        6
    max_score    10
    signal_names ["adherence_drop", "adherence_floor", "skipped_sessions"]
    signal_points [3, 2, 1]
    reasons      [...]
```

This follows the split the codebase already draws. Things that are a deterministic function of the
curated files are materialised during the build — RDF, embeddings, the small field derivations in
`load_member_context`. Things that depend on a *request* are computed at query time and marked
`derived` where they surface as graph structure, as the safety layer's `BLOCKS` edges are
([`backend/app/graph.py`](../backend/app/graph.py)). A churn score reads only `member-context.json`
and the anchor, so it belongs on the build-time side, and materialising it means the copilot
retrieves it like any other fact rather than through a special path.

### The parallel-array shape is a Neo4j constraint

Property values cannot be nested maps, so a `list[ChurnSignal]` cannot be stored as objects. The
signals are split across three index-aligned arrays. `ChurnAssessment.to_graph_props()` is the only
place that flattening happens, and a test pins the arrays to equal length.

### Reading it back

`churn_risk` is a section of the copilot's `member_context` tool and of
`GET /api/members/{id}/context`. The agent is instructed that the `reasons` list is the complete set
of churn facts in the graph and that it may not add signals to it — which is what keeps the
untracked login-frequency claim out of generated answers now that nothing in the graph suggests it.

---

## 6. What this is not

Stating this plainly matters more than the method does.

- **Not a model.** Nothing was trained. The weights are hand-set numbers chosen to be arguable.
- **Not a probability.** "6 of 10" is a count of warning signs, not a 60% chance of anything. The
  agent is instructed to phrase it that way and never to say a member "will churn".
- **Not validated.** There is one member in this dataset and no churn outcomes to check against, so
  these thresholds are reasoned, not fitted. A real deployment would replace the constants with
  values learned from actual retention data — the module is structured so that is a change to one
  table of numbers, not a rewrite.
- **Not a decision.** It ranks who a coach should look at first. It does not decide anything.

The heuristic form is a deliberate choice rather than a placeholder for a "real" model. A coach who
disagrees with an elevated flag can read the three reasons and see exactly which threshold produced
it. That is worth more here than accuracy a coach cannot interrogate.

---

## 7. Known gaps

- **Chat silence is not a signal.** Days since the member's last message is computable from
  `chat_history` and is a genuine disengagement cue. It was left out to keep the first version to
  four signals; it is the most obvious fifth.
- **No login or app-session data.** The one signal the shipped brief asserted is the one signal the
  dataset cannot support. Adding it means adding a field, not adding a rule.
- **Thresholds are global.** A member training twice a week and one training six times are scored
  on the same adherence bands, though `preferences.training_days_per_week` is in the graph and
  could normalise them.
- **One assessment, no history.** The score is recomputed on every build and overwrites. There is no
  record of last week's level, so "is this getting worse?" cannot be answered from the graph.
- **Four weeks minimum for the trend signal.** Members with shorter histories score only on the
  other three, and are therefore slightly harder to flag.
