# Example runs — provenance and filtering traces

Three scenarios through the workout pipeline's deterministic core (member
defaults → constraint resolution → safety traversal), generated from the live
graph by `scripts/example_runs.py`. Every number, rule firing, and graph path
below is real output — no LLM is involved in any of it. In the live system the
one LLM step is plan *composition*, which can only arrange exercises from the
"safe pool" tables below (an output validator rejects anything else), so these
traces are exactly the provenance a generated plan carries.

Regenerate against a running graph with:

```bash
cd backend && uv run python ../scripts/example_runs.py
```


## 1 · Injury case — "Lower-body strength session, 50 minutes"

Member defaults auto-applied from KG 2: five home equipment items, dislikes down-ranked via the resolver, and the injury note resolved to its clinical condition so the rule layer can fire.

**Constraint resolution trace**

- dislike 'Deadlift' resolved to mp_lower_pull_hip_lift (exact) — down-ranked, not excluded
- dislike 'Burpees' resolved to mp_cardio_plyometric (exact) — down-ranked, not excluded
- injury inj_knee_left note resolved to condition cond_pfps (fulltext, score 8.40)

**Filtered out for safety**

- **RNT Split Squat** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Static Jump** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)
- **Dumbbell Goblet Split Squat** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Alternating Dumbbell Racked Crossback Lunge** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Vertical Jump to Broad Jump** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)
- **Jumping Jack** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)

**Not feasible with available equipment** — 29 exercises; substitution suggestions shown for the first three

- **Barbell Decline Bench Press** (missing: Adjustable bench, decline setting, Barbell, Squat / power rack, Weight plate) → alternatives: Dumbbell Neutral-Grip Bench Press, Push-Up to Knee-Drive, Alternating Dumbbell Overhead Press
- **Alternating Dumbbell Decline Bench Press** (missing: Adjustable bench, decline setting) → alternatives: Dumbbell Neutral-Grip Bench Press, Push-Up to Knee-Drive, Alternating Dumbbell Overhead Press
- **Dumbbell Incline Chest Fly** (missing: Adjustable bench, incline setting) → alternatives: Dumbbell Neutral-Grip Bench Press, Push-Up to Knee-Drive

**Safe pool (15 exercises the planner may use)**

| Exercise | Score | Notes |
|---|---:|---|
| Alternating Dumbbell Overhead Press | +0.00 |  |
| Alternating Low Plank To Low Side Plank | +0.00 |  |
| Bench-Lying Single-Arm Dumbbell Tricep Extension | +0.00 |  |
| Bodyweight Pike | +0.00 |  |
| Cow Pose | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| Dumbbell Neutral-Grip Bench Press | +0.00 |  |
| Ground Upper Trap Stretch | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| High Plank Bird Dog | +0.00 |  |
| Low Copenhagen Plank | +0.00 |  |
| Push-Up to Knee-Drive | +0.00 |  |
| Resistance Band Reverse Curl | +0.00 |  |
| Standing Neck Circles | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| Walking Toe Touches | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| World's Greatest Stretch | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| One-Kettlebell Hamstring Walkout | -0.65 | downrank by rule_pfps_downrank_moderate_knee_load (Δ-0.35); down-ranked by member preference (mp_lower_pull_hip_lift, Δ-0.30) — a preference, not a safety exclusion |


## 2 · Limited-equipment case — adjustment: "no barbell, only dumbbells and a kettlebell"

The prior constraint set carries forward; the equipment restriction resolves through the concept resolver and replaces the availability list. Bodyweight exercises always remain feasible; everything else must REQUIRE a subset of {Dumbbell, Kettlebell}.

**Constraint resolution trace**

- dislike 'Deadlift' resolved to mp_lower_pull_hip_lift (exact) — down-ranked, not excluded
- dislike 'Burpees' resolved to mp_cardio_plyometric (exact) — down-ranked, not excluded
- injury inj_knee_left note resolved to condition cond_pfps (fulltext, score 8.40)
- --- adjustment: "no barbell, only dumbbells and a kettlebell" ---
- equipment 'dumbbells' resolved to eq_dumbbell
- equipment 'a kettlebell' resolved to eq_kettlebell
- equipment restricted to ['eq_dumbbell', 'eq_kettlebell'] (bodyweight exercises always qualify)

**Filtered out for safety**

- **Static Jump** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)
- **Dumbbell Goblet Split Squat** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Alternating Dumbbell Racked Crossback Lunge** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Vertical Jump to Broad Jump** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)
- **Jumping Jack** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)

**Not feasible with available equipment** — 42 exercises; substitution suggestions shown for the first three

- **Barbell Decline Bench Press** (missing: Adjustable bench, decline setting, Barbell, Squat / power rack, Weight plate) → alternatives: Alternating Dumbbell Overhead Press
- **Push-Up to Knee-Drive** (missing: Exercise mat) → alternatives: Alternating Dumbbell Overhead Press, Walking Toe Touches
- **Alternating Dumbbell Decline Bench Press** (missing: Adjustable bench, decline setting) → alternatives: Alternating Dumbbell Overhead Press

**Safe pool (3 exercises the planner may use)**

| Exercise | Score | Notes |
|---|---:|---|
| Alternating Dumbbell Overhead Press | +0.00 |  |
| Standing Neck Circles | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| Walking Toe Touches | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |


## 3 · Explicit exclusion — adjustment: "exclude deadlifts"

"Deadlift" matches no catalog exercise name (data quirk 9); the resolver lands it on the hip-hinge movement pattern via curated synonyms, and the pattern exclusion removes every hinge exercise.

**Constraint resolution trace**

- dislike 'Deadlift' resolved to mp_lower_pull_hip_lift (exact) — down-ranked, not excluded
- dislike 'Burpees' resolved to mp_cardio_plyometric (exact) — down-ranked, not excluded
- injury inj_knee_left note resolved to condition cond_pfps (fulltext, score 8.40)
- --- adjustment: "exclude deadlifts" ---
- exclusion 'deadlifts' resolved to mp_lower_pull_hip_lift (fulltext)

**Filtered out for safety**

- **RNT Split Squat** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Static Jump** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)
- **Dumbbell Goblet Split Squat** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Alternating Dumbbell Racked Crossback Lunge** — rule rule_pfps_avoid_loaded_deep_knee_flexion (Patellofemoral contact pressure rises steeply with knee flexion angle under load, and closed-chain loading concentrates it)
  - anatomy path: cond_pfps anchor Structure of patellofemoral joint → Component of knee joint → Knee joint [catalog joint]
- **Vertical Jump to Broad Jump** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)
- **Jumping Jack** — rule rule_pfps_avoid_impact (Repetitive landing impact is the classic aggravator of patellofemoral pain)

**Explicitly excluded**

- **One-Kettlebell Hamstring Walkout** — matches explicitly excluded concept(s): mp_lower_pull_hip_lift
- **Med Ball Hamstring Walkout** — matches explicitly excluded concept(s): mp_lower_pull_hip_lift

**Not feasible with available equipment** — 28 exercises; substitution suggestions shown for the first three

- **Barbell Decline Bench Press** (missing: Adjustable bench, decline setting, Barbell, Squat / power rack, Weight plate) → alternatives: Dumbbell Neutral-Grip Bench Press, Push-Up to Knee-Drive, Alternating Dumbbell Overhead Press
- **Alternating Dumbbell Decline Bench Press** (missing: Adjustable bench, decline setting) → alternatives: Dumbbell Neutral-Grip Bench Press, Push-Up to Knee-Drive, Alternating Dumbbell Overhead Press
- **Dumbbell Incline Chest Fly** (missing: Adjustable bench, incline setting) → alternatives: Dumbbell Neutral-Grip Bench Press, Push-Up to Knee-Drive

**Safe pool (14 exercises the planner may use)**

| Exercise | Score | Notes |
|---|---:|---|
| Alternating Dumbbell Overhead Press | +0.00 |  |
| Alternating Low Plank To Low Side Plank | +0.00 |  |
| Bench-Lying Single-Arm Dumbbell Tricep Extension | +0.00 |  |
| Bodyweight Pike | +0.00 |  |
| Cow Pose | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| Dumbbell Neutral-Grip Bench Press | +0.00 |  |
| Ground Upper Trap Stretch | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| High Plank Bird Dog | +0.00 |  |
| Low Copenhagen Plank | +0.00 |  |
| Push-Up to Knee-Drive | +0.00 |  |
| Resistance Band Reverse Curl | +0.00 |  |
| Standing Neck Circles | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| Walking Toe Touches | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |
| World's Greatest Stretch | +0.00 | explicitly allowed by rule_pfps_allow_unloaded_therapeutic: Unloaded mobility and soft-tissue work over an irritable joint is indicated, not contraindicated. This rule is the direct answer to the false positives a joints_loaded filter produces. |

