# Data Overview

A field-by-field picture of the two synthetic datasets in [`data/`](../data), plus the value
distributions, the places they join, and the quirks that will bite a naive knowledge-graph loader.

| Dataset | Shape | Size | Role |
|---|---|---|---|
| [`data/exercises.json`](../data/exercises.json) | JSON array of 50 objects | 30 KB | Exercise catalog — the movement/clinical side of the graph |
| [`data/member-context.json`](../data/member-context.json) | Single JSON object, 11 sections | 4.7 KB | One member (Jordan Rivera) — the member-context side of the graph |

---

## 1. `exercises.json` — exercise catalog

### 1.1 Schema

Every one of the 50 records has all 14 keys — no optional fields, no ragged objects.

| Field | Type | Distinct values | Notes |
|---|---|---|---|
| `id` | UUID string | 50 | Unique. Primary key. |
| `name` | string | 50 | Unique. Human-readable, e.g. `"Kettlebell Goblet Cyclist Squat"`. |
| `muscle_groups` | string[] | 19 | 1–4 per exercise. Free-text taxonomy, no IDs. |
| `joints_loaded` | string[] | 9 | 0–4 per exercise. **The injury-safety join key.** |
| `movement_patterns` | string[] | 36 | 1–4 per exercise. `"category - subtype"` convention, loosely enforced. |
| `equipment_required` | string[] | 32 | 0–4 per exercise. **The feasibility join key.** |
| `is_bilateral` | bool | 2 | 18 true / 32 false. Semantics are inverted — see [Quirks](#4-quirks-and-gotchas). |
| `side` | string \| null | 4 | `null` (32), `left_leg` (7), `left_side` (6), `left_arm` (5). |
| `priority_tier` | int | **1** | Always `2`. Carries zero signal. |
| `is_reps` | bool | 2 | 42 true / 8 false. |
| `is_duration` | bool | **1** | Always `true`. Carries zero signal. |
| `supports_weight` | bool | 2 | 28 true / 22 false — can external load be added. |
| `estimated_rep_duration` | float | 12 | Seconds per rep, `0`–`1.9`. `0` exactly for the 8 `is_reps: false` rows. |
| `bilateral_pair_id` | UUID \| null | 19 | Non-null on the same 18 rows where `side` is set. **All 18 are dangling.** |

### 1.2 Value distributions

**Muscle groups** (19 distinct, counts across 50 exercises)

| Muscle group | n | Muscle group | n | Muscle group | n |
|---|--:|---|--:|---|--:|
| deltoids | 15 | lats | 7 | traps | 3 |
| glutes | 13 | calves | 7 | forearms | 2 |
| core | 12 | chest | 5 | hip adductors | 2 |
| quads | 12 | hip flexors | 5 | lower back | 2 |
| triceps | 8 | hamstrings | 4 | rotator cuff | 1 |
| biceps | 8 | middle back | 3 | | |
| upper back | 8 | obliques | 3 | | |

**Joints loaded** (9 distinct) — the vocabulary an injury filter has to match against

| Joint | n | Joint | n |
|---|--:|---|--:|
| shoulder | 25 | wrist | 7 |
| hip | 25 | cervical spine | 3 |
| knee | 21 | thoracic spine | 2 |
| ankle | 21 | lumbar spine | 2 |
| elbow | 19 | | |

**Movement patterns** (36 distinct — a long tail of singletons)

| Pattern | n | Pattern | n | Pattern | n |
|---|--:|---|--:|---|--:|
| cardio | 8 | lower pull - hip lift | 2 | lower push - step-up | 1 |
| regen | 6 | legs - accessory | 2 | quadruped | 1 |
| cardio - plyometric | 6 | lower push - lunge | 2 | core - carry | 1 |
| arms - accessory | 6 | lower - adduction | 2 | lower push - calf raise | 1 |
| upper push - horizontal | 5 | core - anti-lateral flexion | 2 | massage | 1 |
| mobility - dynamic | 5 | upper pull - horizontal | 2 | core - rotation | 1 |
| core - anti-extension | 4 | mobility - static | 2 | yoga | 1 |
| upper pull - vertical | 4 | upper - adduction | 1 | total body | 1 |
| core - flexion | 3 | upper push - vertical | 1 | `car` | 1 |
| isometric | 3 | core - extension | 1 | | |
| lower push - squat | 3 | lower - abduction | 1 | | |
| lower push - split squat | 3 | | | | |
| core - anti-rotation | 3 | | | | |
| cardio - locomotion | 3 | | | | |
| shoulders - accessory | 2 | | | | |
| balance | 2 | | | | |

**Equipment** (32 distinct; 5 exercises need none)

| Equipment | n | Equipment | n |
|---|--:|---|--:|
| Yoga Mat | 11 | Kettlebell | 2 |
| Dumbbell | 9 | Rack | 2 |
| Barbell | 3 | Preacher Curl Bench | 2 |
| Plate | 3 | Adjustable Bench - Decline | 2 |
| Adjustable Bench - Incline | 3 | Pull-Up Bar | 2 |
| Flat Bench | 3 | _22 machines/implements_ | 1 each |
| Resistance Band - Loop | 3 | | |
| Medicine Ball | 3 | | |

The 22 singletons: Seated Lat Pulldown Machine, Stability Ball, EZ Bar, Cable Resistance Machine,
Handle Attachment, Suspension Trainer, Slant Board, Stair Climber, Box, Sandbag, BOSU, Horizontal
Leg Press Machine, Jump Rope, Lacrosse Ball, Wall, Chest Supported Row Machine, Resistance Band -
With Handles, SkiErg, Miniband.

### 1.3 Full catalog

`Flags` collapses `is_bilateral` / `side` / `is_reps` / `supports_weight`. `Rep sec` is
`estimated_rep_duration`.

| Exercise | Muscle groups | Joints loaded | Movement patterns | Equipment | Flags | Rep sec |
|---|---|---|---|---|---|--:|
| Alternating Dumbbell Decline Bench Press | chest, triceps | _(none)_ | upper push - horizontal | Adjustable Bench - Decline, Dumbbell | loadable | 0.5 |
| Alternating Dumbbell Overhead Press | deltoids, triceps | shoulder, elbow | upper push - vertical | Dumbbell | loadable | 0.4 |
| Alternating Dumbbell Racked Crossback Lunge | glutes, quads, hip adductors | hip, knee, ankle | lower push - lunge, lower - abduction, lower - adduction | Dumbbell | loadable | 0.5 |
| Alternating Low Plank To Low Side Plank | core, obliques, deltoids | shoulder, elbow, shoulder | core - anti-lateral flexion, core - anti-extension | Yoga Mat | - | 0.1 |
| Anchored Band Rotational Lift | obliques, deltoids | shoulder, thoracic spine | core - rotation, shoulders - accessory | Resistance Band - With Handles | bilateral, left_side, loadable | 0.4 |
| BOSU Step Over | core, calves, quads, glutes | ankle, hip, knee, elbow | cardio - plyometric, cardio | BOSU | - | 0.7 |
| Band-Assisted Chin-Up (From Foot) | lats, middle back, biceps, upper back | shoulder, elbow, wrist | upper pull - vertical | Pull-Up Bar, Resistance Band - Loop | loadable | 0.2 |
| Barbell Decline Bench Press | chest, triceps | shoulder, elbow | upper push - horizontal | Adjustable Bench - Decline, Barbell, Plate, Rack | loadable | 0.3 |
| Barbell Racked Forward Lunge | glutes, quads | hip, knee, ankle | lower push - lunge | Barbell, Plate, Rack | bilateral, left_leg, loadable | 0.5 |
| Barbell Step Up to Knee-Drive | glutes, quads, hip flexors, core | ankle, knee, hip | lower push - step-up, core - flexion, balance | Barbell, Box, Plate | bilateral, left_leg, loadable | 0.6 |
| Bench-Lying Single-Arm Dumbbell Tricep Extension | triceps | elbow, shoulder | arms - accessory | Dumbbell, Flat Bench | bilateral, left_arm, loadable | 0.3 |
| Bench-Supported Incline YTI | deltoids, upper back, rotator cuff | shoulder, cervical spine | mobility - dynamic, shoulders - accessory, balance, core - extension | Adjustable Bench - Incline | - | 0.2 |
| Bodyweight Pike | deltoids, core, hip flexors | hip, shoulder, wrist | core - flexion, core - anti-extension | Yoga Mat | - | 0.3 |
| Cow Pose | lower back | lumbar spine, hip, knee, ankle | mobility - static, yoga, regen | Yoga Mat | duration-only | 0 |
| Dumbbell Goblet Split Squat | quads, glutes | hip, knee, ankle | lower push - split squat | Dumbbell | bilateral, left_leg, loadable | 0.3 |
| Dumbbell Incline Chest Fly | chest | shoulder | upper push - horizontal, upper - adduction | Dumbbell, Adjustable Bench - Incline | loadable | 0.3 |
| Dumbbell Neutral-Grip Bench Press | chest, triceps | wrist, elbow, shoulder | upper push - horizontal | Dumbbell, Flat Bench | loadable | 0.2 |
| Ground Upper Trap Stretch | traps | cervical spine | mobility - static, regen | Yoga Mat | bilateral, left_side, duration-only | 0 |
| High Plank Bird Dog | core, deltoids | wrist, shoulder, knee | core - anti-rotation, isometric, quadruped | Yoga Mat | - | 0.2 |
| Horizontal Leg Press Calf Raises | calves | ankle | lower push - calf raise | Horizontal Leg Press Machine | loadable | 0.5 |
| Isometric Pull-Up | lats, middle back, biceps, upper back | shoulder, elbow, wrist | upper pull - vertical, isometric | Pull-Up Bar | duration-only | 0 |
| Jump Rope - Single-Leg | calves | ankle | cardio - plyometric, cardio | Jump Rope | bilateral, left_leg | 1.9 |
| Jumping Jack | calves, deltoids | ankle, shoulder, hip | cardio - plyometric, cardio | _(bodyweight)_ | - | 0.7 |
| Kettlebell Goblet Cyclist Squat | quads, glutes | hip, knee, ankle | lower push - squat | Kettlebell, Slant Board | loadable | 0.3 |
| Kneeling Stability Ball Lat Stretch | lats, upper back | shoulder, knee, hip | mobility - dynamic, regen | Stability Ball, Yoga Mat | bilateral, left_side, duration-only | 0.2 |
| Lacrosse Ball Upper Back against Wall | upper back | _(none)_ | massage, regen | Lacrosse Ball, Wall | bilateral, left_side, duration-only | 0 |
| Low Copenhagen Plank | obliques, hip adductors, deltoids | ankle, knee, hip, shoulder | core - anti-lateral flexion, lower - adduction, isometric | Flat Bench, Yoga Mat | bilateral, left_side, duration-only | 0 |
| Machine - Chest-Supported Row | upper back, lats, biceps, deltoids | elbow, shoulder | upper pull - horizontal | Chest Supported Row Machine | loadable | 0.3 |
| Machine - Single-Arm Lat Pull-Down | lats, biceps, deltoids | shoulder, elbow | upper pull - vertical | Seated Lat Pulldown Machine | bilateral, left_arm, loadable | 0.5 |
| Med Ball Hamstring Walkout | hamstrings, glutes, core | knee, hip, ankle | lower pull - hip lift, legs - accessory, core - anti-rotation | Medicine Ball, Yoga Mat | loadable | 0.4 |
| Med Ball Scoop Toss | deltoids, core, quads, glutes | elbow, hip, knee, shoulder | lower push - squat, upper pull - vertical, cardio - plyometric, cardio | Medicine Ball | bilateral, left_side, loadable | 1.1 |
| Med Ball Split Squat | quads, glutes | hip, knee, ankle | lower push - split squat | Medicine Ball | bilateral, left_leg, loadable | 0.5 |
| One-Kettlebell Hamstring Walkout | hamstrings, glutes, core | knee, hip, ankle | lower pull - hip lift, legs - accessory, core - anti-rotation | Kettlebell, Yoga Mat | loadable | 0.3 |
| Push-Up to Knee-Drive | chest, triceps, deltoids, core | shoulder, elbow, wrist | upper push - horizontal, core - anti-extension, core - flexion | Yoga Mat | - | 1.2 |
| RNT Split Squat | quads, glutes | hip, knee, ankle | lower push - split squat | Resistance Band - Loop | bilateral, left_leg, loadable | 0.8 |
| Resistance Band Reverse Curl | biceps, forearms | elbow | arms - accessory | Resistance Band - Loop | loadable | 0.3 |
| Sandbag Zercher Carry | core, forearms, traps | ankle, knee, hip, shoulder | core - carry, cardio - locomotion | Sandbag | duration-only, loadable | 0 |
| Single-Arm Cable Tricep Extension | triceps | elbow | arms - accessory | Cable Resistance Machine, Handle Attachment | bilateral, left_arm, loadable | 0.4 |
| Single-Arm Chest-Supported Incline Row | upper back, lats, biceps, deltoids | elbow, shoulder | upper pull - horizontal | Dumbbell, Adjustable Bench - Incline | bilateral, left_arm, loadable | 0.5 |
| Single-Arm Dumbbell Preacher Curl | biceps | elbow | arms - accessory | Preacher Curl Bench, Dumbbell | bilateral, left_arm, loadable | 0.2 |
| SkiErg | lower back, lats, deltoids, core | elbow, wrist, shoulder, hip | cardio - locomotion, cardio, total body | SkiErg | - | 0.6 |
| Stair Climber | glutes, calves, quads, hip flexors | ankle, hip, knee | cardio - locomotion, cardio | Stair Climber | duration-only | 0 |
| Standing Miniband Hip Flexion | hip flexors | hip, knee, ankle | car | Miniband | bilateral, left_leg, loadable | 0.5 |
| Standing Neck Circles | traps, upper back | cervical spine | mobility - dynamic, regen | _(bodyweight)_ | - | 0.1 |
| Static Jump | quads, calves | ankle, hip, knee | cardio - plyometric, cardio | _(bodyweight)_ | - | 0.3 |
| Suspension Tricep Press | triceps | shoulder, elbow, hip | arms - accessory, core - anti-extension | Suspension Trainer | - | 0.2 |
| Vertical Jump to Broad Jump | glutes, quads, calves | ankle, hip, knee | cardio - plyometric, cardio, lower push - squat | _(bodyweight)_ | - | 0.3 |
| Walking Toe Touches | hamstrings, core, deltoids | lumbar spine, shoulder, hip | mobility - dynamic | _(bodyweight)_ | - | 0.5 |
| Wide-Grip Preacher Curl with EZ Bar | biceps | elbow | arms - accessory | EZ Bar, Preacher Curl Bench | loadable | 0.3 |
| World's Greatest Stretch | hamstrings, middle back, hip flexors | hip, thoracic spine, knee, ankle | mobility - dynamic, regen | Yoga Mat | - | 0.1 |

---

## 2. `member-context.json` — member context

One member, eleven top-level sections. `_note` is a disclaimer string; the rest is data.

| Section | Shape | Count | What it feeds |
|---|---|--:|---|
| `profile` | object | 10 fields | Copilot identity, demographic scaling |
| `goals` | array of objects | 3 | Workout intent, prioritization |
| `preferences` | object | 5 fields | Session length, day scheduling, exclusions |
| `equipment_available` | string[] | 5 | Hard feasibility filter on the catalog |
| `injuries` | array of objects | 1 | Hard safety filter + ontology hook |
| `workout_history` | array of objects | 4 | Recency, volume, RPE trend |
| `adherence` | object | 4 weeks + trend | Churn signal — two of the four scored signals read this |
| `biomarkers` | object | 4 metrics | Readiness / recovery |
| `labs` | object | 2 panels | Long-horizon health context |
| `chat_history` | array of objects | 4 | Retrieval corpus for the copilot |
| `coach_brief` | object | 2 tasks + churn risk | Morning-brief surface; the churn block is **not ingested** (q11) |

### 2.1 Profile

| Field | Value |
|---|---|
| `id` | `mbr_01HX9JORDAN` |
| `name` | Jordan Rivera |
| `age` | 41 |
| `sex` | female |
| `height_cm` | 168 |
| `weight_kg` | 71.2 |
| `timezone` | America/Los_Angeles |
| `member_since` | 2024-09-15 |
| `coach_id` | `coach_01HXSAM` |
| `tier` | 1:1 Coaching |

### 2.2 Goals

| id | Text | Priority | Target date |
|---|---|--:|---|
| `goal_strength` | Build lower-body strength | 1 | 2026-09-01 |
| `goal_knee` | Return to pain-free squatting after left-knee flare-up | 1 | 2026-07-15 |
| `goal_sleep` | Average 7+ hours of sleep on weeknights | 2 | `null` |

Two goals tie for priority 1, and `goal_knee` directly contradicts an unfiltered reading of
`goal_strength` — the generator has to reconcile them rather than pick one.

### 2.3 Preferences and equipment

| Field | Value |
|---|---|
| `preferred_session_minutes` | 50 |
| `training_days_per_week` | 4 |
| `preferred_days` | Mon, Wed, Thu, Sat |
| `dislikes` | Deadlift, Burpees |
| `notes` | Prefers dumbbell and kettlebell work; trains at home. Dislikes high-impact jumping. |
| `equipment_available` | Dumbbell, Kettlebell, Yoga Mat, Resistance Band - Loop, Flat Bench |

### 2.4 Injuries

| Field | Value |
|---|---|
| `id` | `inj_knee_left` |
| `region` / `joint` | left knee / `knee` |
| `status` | recovering |
| `severity` | mild |
| `since` | 2026-05-10 |
| `notes` | Patellofemoral pain after a hiking trip. Cleared for low-impact loading; avoid deep knee flexion under load and plyometrics. |
| `snomedct_hint` | Look up patellofemoral pain syndrome / knee joint structures in SNOMED CT via NCI EVS. |

`joint: "knee"` is the only structured field that joins to the catalog. Everything else that matters
— "avoid deep knee flexion under load", "avoid plyometrics" — lives in the free-text `notes`.

### 2.5 Workout history

| Date | Title | Completed | Min | RPE | Exercises |
|---|---|---|--:|--:|---|
| 2026-06-03 | Lower Body - Bands & DB | yes | 28 | 6 | Goblet Squat (box-supported), Hip Thrust, Banded Lateral Walk |
| 2026-06-01 | Upper Body Push | yes | 31 | 7 | DB Floor Press, Half-Kneeling DB Press, Band Pull-Apart |
| 2026-05-29 | Full Body | **no** | 0 | `null` | _(none)_ |
| 2026-05-27 | Lower Body | yes | 26 | 6 | Step-Up, KB Romanian Deadlift, Wall Sit |

Sessions run 26–31 min against a stated 50-min preference.

### 2.6 Adherence and biomarkers

| Week of | Completion % |
|---|--:|
| 2026-05-12 | 100 |
| 2026-05-19 | 100 |
| 2026-05-26 | 75 |
| 2026-06-02 | 50 |

`trend: "declining"`.

| Biomarker | Value |
|---|---|
| `resting_hr_bpm` | 58 |
| `hrv_ms` | 47 |
| `sleep_hours_last_7_days` | 6.1, 5.4, 7.2, 6.0, 5.1, 7.8, 6.3 (mean 6.27) |
| `weight_trend_kg` | 72.4 (05-05) → 71.9 (05-19) → 71.2 (06-02) |

Mean sleep of 6.27 h sits under the 7 h target in `goal_sleep`.

### 2.7 Labs

| Blood panel (2026-04-20) | Value | DEXA scan (2026-03-30) | Value |
|---|--:|---|--:|
| LDL mg/dL | 118 | Body fat % | 29.4 |
| HDL mg/dL | 61 | Lean mass kg | 47.1 |
| Triglycerides mg/dL | 96 | Fat mass kg | 21.0 |
| HbA1c % | 5.3 | Bone density Z-score | 0.4 |
| Vitamin D ng/mL | 28 | Visceral fat cm² | 78 |
| Ferritin ng/mL | 41 | | |
| CRP mg/L | 1.2 | | |

No reference ranges ship with the data — any "low vitamin D" style claim has to source its
thresholds elsewhere and cite them.

### 2.8 Chat history

| Timestamp | From | Text |
|---|---|---|
| 2026-06-03T18:42-07:00 | member | Knocked out the lower body session! Knee felt okay with the box squats. |
| 2026-06-03T19:05-07:00 | coach | Love it — that's the green light we wanted. How's the knee this morning vs. after? |
| 2026-05-30T08:12-07:00 | member | Skipped Thursday, work blew up and I was wiped. Sorry! |
| 2026-05-22T07:50-07:00 | member | Still no barbell at home btw — only DBs and a kettlebell. _(+ image attachment)_ |

Only the last message carries `attachments` — the field is absent, not `null`, on the other three.

### 2.9 Coach brief

Generated for 2026-06-04.

| Type | Text |
|---|---|
| `celebrate` | Congratulate Jordan on completing yesterday's lower-body session — first pain-free squat work since the knee flare-up. |
| `review_risk` | Check churn risk: adherence dropped 100% → 50% over the last two weeks. |

Churn risk: **elevated**, for three reasons — adherence fell 100% → 50% over 2 weeks; one skipped
session with a fatigue/work explanation; login frequency down vs. prior month. The third reason has
no backing field anywhere in the dataset.

Because of that third reason this block is **not loaded into the graph**. Churn risk is computed
from `adherence` and `workout_history` instead, onto a `:ChurnAssessment` node — the sample member
scores 6 of 10 and still lands on `elevated`, but with three reasons that each name a field that
exists. Method: [`docs/churn-risk-classification.md`](./churn-risk-classification.md).

---

## 3. Where the two datasets join

There is no shared identifier. Every link is a string match on a shared vocabulary.

| Bridge | Member side | Catalog side | Match type |
|---|---|---|---|
| Equipment | `equipment_available[]` | `equipment_required[]` | Exact string, subset test |
| Injury | `injuries[].joint` | `joints_loaded[]` | Exact string |
| Contraindication | `injuries[].notes` (prose) | `movement_patterns[]` | **None** — needs a model or a hand-built map |
| Dislikes | `preferences.dislikes[]` | `name` | **None** — zero literal matches |
| History | `workout_history[].exercises[]` | `name` | **None** — zero literal matches |

Applying the two bridges that do work, against Jordan's five pieces of equipment:

| Filter | Surviving exercises |
|---|--:|
| All | 50 |
| Equipment-feasible | **21** |
| …of which load the knee | 10 |
| …of which are plyometric | 3 |

The 21 equipment-feasible exercises:

| Exercise | Equipment | Loads knee | Plyometric |
|---|---|:--:|:--:|
| Push-Up to Knee-Drive | Yoga Mat | | |
| Dumbbell Neutral-Grip Bench Press | Dumbbell, Flat Bench | | |
| Alternating Dumbbell Overhead Press | Dumbbell | | |
| Bodyweight Pike | Yoga Mat | | |
| Resistance Band Reverse Curl | Resistance Band - Loop | | |
| Bench-Lying Single-Arm Dumbbell Tricep Extension | Dumbbell, Flat Bench | | |
| RNT Split Squat | Resistance Band - Loop | yes | |
| Static Jump | _(bodyweight)_ | yes | yes |
| Dumbbell Goblet Split Squat | Dumbbell | yes | |
| One-Kettlebell Hamstring Walkout | Kettlebell, Yoga Mat | yes | |
| World's Greatest Stretch | Yoga Mat | yes | |
| Walking Toe Touches | _(bodyweight)_ | | |
| Alternating Dumbbell Racked Crossback Lunge | Dumbbell | yes | |
| Vertical Jump to Broad Jump | _(bodyweight)_ | yes | yes |
| Alternating Low Plank To Low Side Plank | Yoga Mat | | |
| High Plank Bird Dog | Yoga Mat | yes | |
| Jumping Jack | _(bodyweight)_ | | yes |
| Low Copenhagen Plank | Flat Bench, Yoga Mat | yes | |
| Cow Pose | Yoga Mat | yes | |
| Ground Upper Trap Stretch | Yoga Mat | | |
| Standing Neck Circles | _(bodyweight)_ | | |

Note how blunt `joints_loaded: knee` is as a safety filter: it flags Cow Pose and World's Greatest
Stretch — both explicitly appropriate for a recovering knee — while the injury note's actual
contraindications (deep knee flexion under load, plyometrics) are only recoverable from
`movement_patterns`. A safety layer built on `joints_loaded` alone either bans harmless mobility
work or lets jumps through, depending on which way it errs.

---

## 4. Quirks and gotchas

| # | Issue | Where | Impact |
|---|---|---|---|
| 1 | `is_bilateral: true` is set on exactly the 18 exercises that are **unilateral** (they all carry a `side` and a `bilateral_pair_id`, and the names say "Single-Arm", "Split Squat", "Single-Leg"). The flag reads as "has a bilateral pair", not "is performed bilaterally". | `exercises.json` | Trusting the field name inverts every left/right prescription. |
| 2 | All 18 `bilateral_pair_id` values point at UUIDs that **do not exist** in the file. | `exercises.json` | Any pair-resolution join returns nothing. The right-side counterparts were never shipped. |
| 3 | `priority_tier` is `2` for all 50 rows; `is_duration` is `true` for all 50. | `exercises.json` | Zero-entropy fields. Not usable for ranking or filtering. |
| 4 | 2 exercises have an empty `joints_loaded`: Alternating Dumbbell Decline Bench Press, Lacrosse Ball Upper Back against Wall. | `exercises.json` | They pass every injury filter by default. Decide whether missing means safe or unknown. |
| 5 | `movement_patterns` includes `"car"` (Standing Miniband Hip Flexion) — almost certainly a truncated `"cars"` / controlled articular rotation, not a vehicle. | `exercises.json` | An embedding-based pattern matcher will place it nonsensically. |
| 6 | `"Alternating Low Plank To Low Side Plank"` lists `shoulder` twice in `joints_loaded`. | `exercises.json` | Dedupe before counting or weighting. |
| 7 | 36 movement patterns over 50 exercises, 19 of them singletons; the `"category - subtype"` convention breaks on `regen`, `isometric`, `balance`, `yoga`, `massage`, `quadruped`, `total body`, `car`. | `exercises.json` | Splitting on `" - "` to build a hierarchy yields a lopsided tree. |
| 8 | Every unilateral exercise is `left_*`. There is no `right_*` anywhere. | `exercises.json` | A per-side program has to synthesize the right side. |
| 9 | `dislikes` are `Deadlift` and `Burpees` — **neither appears in the catalog**, and the nearest relatives (KB Romanian Deadlift in history, the three plyometric jumps) don't match by string. | Cross-dataset | Literal exclusion is a no-op. Needs semantic matching. |
| 10 | All 9 exercise names in `workout_history` are **absent from the catalog** (Goblet Squat (box-supported), Hip Thrust, Banded Lateral Walk, DB Floor Press, …). | Cross-dataset | History cannot be joined to the catalog by name. Treat it as free text, or map it. |
| 11 | Churn reason "login frequency down vs. prior month" has no supporting field in the data. | `member-context.json` | The copilot would either hallucinate a number or cite the brief as its own source. **Resolved** by dropping the file's churn block and computing the level from adherence + workout history instead ([method](./churn-risk-classification.md)). |
| 12 | Lab panels ship values with no reference ranges or units metadata beyond the key suffixes. | `member-context.json` | Any "high/low/normal" verdict requires an external, citable source. |
| 13 | Dates run to mid-2026 and `injuries.since` is 2026-05-10; the brief is generated for 2026-06-04. | `member-context.json` | Don't compute recency against the real wall clock — anchor to 2026-06-04. |
| 14 | `attachments` is present on one chat message and absent (not `null`) on the rest. | `member-context.json` | Use `.get()` semantics; a strict schema with a required key fails. |
