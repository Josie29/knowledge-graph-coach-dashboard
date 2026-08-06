# KG 1 — Movement / Clinical Domain Graph

Schema and ontology-subset curation for the movement side of the coach dashboard: every node type,
every edge type, what was pulled from each published ontology, and — at greater length — what was
deliberately left out.

The curated data lives in [`data/ontology/`](../data/ontology). This document explains it.

> **Headline.** The catalog's 96 vocabulary terms (19 muscle groups, 9 joints, 36 movement patterns,
> 32 equipment) are all mapped, plus 16 clinical conditions. But only 44 of those 112 concepts reach a
> `skos:exactMatch` in any published ontology. That ratio is the finding, not a shortfall — see
> [§5](#5-what-we-left-out-and-why).

---

## 1. Why this graph exists

The brief's test is whether the graph does real work or is "semantic search with extra steps". The
concrete target is the criticism in [`docs/data-overview.md §3`](./data-overview.md): filtering the
catalog on `joints_loaded` contains `"knee"` is a bad safety filter, and it is bad in *both*
directions at once.

For the sample member — Jordan Rivera, recovering mild patellofemoral pain in the left knee, five
pieces of home equipment — the numbers are these:

| Filter | Excluded | Wrong how |
|---|--:|---|
| `joints_loaded` contains `knee` | 10 of 21 | Bans Cow Pose and World's Greatest Stretch, both explicitly appropriate for a recovering knee. Lets Jumping Jack through, which her injury note rules out. |
| This graph's contraindication rules | 6 of 21 | — |

The rule layer **rescues 5** exercises the blunt filter bans and **catches 1** it misses. That single
catch, Jumping Jack, is the whole argument in miniature: it is plyometric, the member's note says
"avoid plyometrics", and its `joints_loaded` array is `[ankle, shoulder, hip]`. No anatomy-based
filter can ever reach it. A mechanics-based one reaches it immediately.

---

## 2. Node types

Nine node types. Five are catalog vocabularies, one is the catalog itself, three are the clinical and
reasoning layer.

| Node | Count | Key | Source | Purpose |
|---|--:|---|---|---|
| `Exercise` | 50 | catalog UUID | `data/exercises.json` | The prescribable unit |
| `MuscleGroup` | 19 | `mg_*` | `muscle_groups` field | Targeting and emphasis |
| `Joint` | 9 | `jt_*` | `joints_loaded` field | The injury-safety join key |
| `MovementPattern` | 36 | `mp_*` | `movement_patterns` field | Carries the biomechanical attributes rules match on |
| `Equipment` | 32 | `eq_*` | `equipment_required` field | Feasibility and substitution |
| `AnatomicalStructure` | 173 | SNOMED code | SNOMED CT via NCI EVS | The partonomy joints and muscles hang from |
| `Condition` | 16 | `cond_*` | curated | Injuries and clinical presentations |
| `SafetyRule` | 22 | `rule_*` | curated | Condition → unsafe (or safe) movement |
| `Concept` | — | mixin | — | Anything resolvable from free text; carries `prefLabel`, `altLabels`, embedding |

`Concept` is a label applied across the vocabulary nodes rather than a separate node. Every muscle
group, joint, pattern, equipment item and condition is a `Concept`, which is what lets the three-pass
resolver query one index instead of five.

### Why `AnatomicalStructure` is separate from `Joint`

`Joint` holds the 9 strings the catalog actually uses. `AnatomicalStructure` holds the 173 SNOMED
body structures beneath and around them — the patellofemoral joint, the meniscus, the anterior
cruciate ligament, the lumbar intervertebral disc. Keeping them distinct means the catalog's coarse
vocabulary is never polluted by clinical granularity it cannot support, while injuries can still be
recorded at whatever precision the clinician used.

---

## 3. Edge types

| Edge | From → To | Cardinality | Semantics |
|---|---|---|---|
| `TARGETS` | Exercise → MuscleGroup | 1–4 | The exercise trains this muscle. Aligns to `ope:engages`. |
| `STRESSES` | Exercise → Joint | 0–4 | The exercise loads this joint. The injury-safety join. |
| `REQUIRES` | Exercise → Equipment | 0–4 | Hard feasibility constraint. Aligns to `ope:utilizesEquipment`. |
| `HAS_PATTERN` | Exercise → MovementPattern | 1–4 | What the movement *is*. Carries mechanics into the rules. |
| `PART_OF` | AnatomicalStructure → AnatomicalStructure | many | Mereological: the child is a component of the parent. |
| `IS_A` | AnatomicalStructure → AnatomicalStructure | many | Taxonomic: the child is a kind of the parent, usually a laterality refinement. |
| `ANCHORED_AT` | Condition → AnatomicalStructure | 1 | The structure the condition is about. |
| `CONTRAINDICATED_FOR` | SafetyRule → Condition | 1 | The rule applies to this condition. |
| `SUBSTITUTABLE_FOR` | Equipment → Equipment | many | Directed, degreed: `equivalent` / `close` / `partial`. |

### `PART_OF` and `IS_A` are two edges, not one

SNOMED CT's stated body-structure hierarchy is a single IS-A tree carrying both readings. "Structure
of left knee joint" is a *kind of* knee joint; "Structure of patellofemoral joint" is a *part of*
one. Collapsing them is convenient and wrong — a rule written against a part would inherit down a
specialisation, and a laterality claim would leak to the other limb.

Every one of the 158 edges was hand-classified: **130 `PART_OF`, 28 `IS_A`**. Each carries a `basis`
field recording whether SNOMED asserted the link (`snomed_isa`, 156 edges) or whether this project
did (`curated`, 2 edges). A reviewer can see exactly where our judgement was added.

### `STRESSES` is not trusted absolutely

Two catalog exercises have an empty `joints_loaded` array and would pass any anatomy-gated rule by
default. Jumping Jack omits the knee despite being a bilateral landing task. So the rule engine can
fire on movement mechanics *without* requiring anatomical overlap — see
[§6](#6-contraindication-rules).

---

## 4. The anatomy closure

The catalog tags exercises at exactly 9 coarse joints. An injury may be recorded far below that
granularity, or laterally. The closure bridges the gap:

```cypher
MATCH (injury:Concept)-[:PART_OF|IS_A*0..5]->(anc)
MATCH (joint:Concept {is_catalog_joint: true})
WHERE joint = anc OR (joint)-[:PART_OF]->(anc)
MATCH (ex:Exercise)-[:STRESSES]->(joint)
```

Two design points, both of which the validator caught rather than the author:

**Depth is bounded at 5.** The deepest catalog-relevant chain is the lumbar disc: nucleus pulposus →
lumbar intervertebral disc → lumbar intervertebral symphysis → lumbar spine joint → lumbar spine
joint region → lumbar spine. The ACL needs 4. Bounding at 5 admits both while stopping the walk
short of `Structure of vertebral column`, which would drag in every spinal exercise.

**One optional step back down.** Tendon and muscle structures hang off a body *region*, not off the
joint — the rotator cuff under the shoulder region, the Achilles under its own region, the common
extensor origin under the elbow region. Upward-only traversal never connects those conditions to a
catalog joint at all; four of the sixteen conditions were silently unreachable until this was added.
The step is bounded at one hop, because two would let a knee condition descend from
`Structure of vertebral column` into every spinal exercise.

Every condition's declared hop count and route is machine-checked against the graph.

### The two required resolutions

The brief names two specific cases. Both work:

- **"her left knee"** → `snomed:719442003` (Structure of left knee joint) `IS_A` `snomed:49076000`
  (Knee joint structure, catalog-tagged). Sub-structures are covered in the other direction: a
  patellofemoral diagnosis reaches the same joint in 2 hops, an ACL injury in 4.
- **"bad lower back"** → `snomed:122496007` (Lumbar spine structure), which *is* the catalog joint.

"lower back" is genuinely ambiguous — it is both a catalog `muscle_groups` value and how a coach
names a lumbar complaint, and the two readings resolve to different SNOMED concepts. The resolution
policy is context-dependent and recorded in `anatomy-partonomy.json → ambiguous_surface_forms`:
injury context resolves to the spinal segment, targeting context to
`snomed:48144002` (Structure of muscle of lower back). Where the context is unknown the resolver
returns both with a flag rather than guessing.

---

## 5. What we left out, and why

This is the part of the exercise that matters, so it gets the space.

### 5.1 Coverage, honestly

| Vocabulary | n | Reaches `exactMatch` | Broader/close only | Related-only or unmapped | SKOS triples |
|---|--:|--:|--:|--:|--:|
| Joints | 9 | **9** | 0 | 0 | 13 |
| Muscle groups | 19 | **17** | 2 | 0 | 45 |
| Conditions | 16 | **15** | 1 (close) | 0 | 39 |
| Movement patterns | 36 | **3** | 28 | 5 | 89 |
| Equipment | 32 | **0** | 31 | 1 | 48 |
| **Total** | **112** | **44** | **62** | **6** | **234** |

The gradient is the story. Anatomy and pathology are *superbly* covered by SNOMED CT. Movement and
equipment are barely covered by anything.

### 5.2 SNOMED CT — pulled heavily

**What we pulled:** 173 body structures and 16 conditions, all verified live against the NCI EVS
REST API (`snomedct_us`, version 2025_09_01). Anatomy for all 9 joints plus their sub-structures,
muscle structures for all 19 muscle groups, laterality variants, and the condition concepts the rules
hang from.

**Why it earns primacy:** SNOMED has genuine *functional group* concepts that a lay taxonomy needs
and most anatomies lack — `Flexor of hip joint` (303800000), `Structure of adductor muscle of hip
joint` (303803003), `Muscle of buttock` (102291007). A colloquial term like "hip flexors" lands on a
first-class concept instead of a hand-rolled bag of muscles. That single property decided which
vocabulary was primary.

**What we left out:** essentially all of it. SNOMED CT has ~350,000 active concepts; we took 189. No
procedures, no pharmaceuticals, no findings beyond the 16 musculoskeletal conditions, no organism or
substance hierarchies. We also import no SNOMED distribution files — only concept identifiers and
preferred terms, which is the minimum an alignment needs and keeps the repository clear of licensed
content.

**Where SNOMED failed us**, recorded rather than papered over:

- **No "middle back" muscle concept.** SNOMED partitions back musculature into upper (41777002) and
  lower (48144002) with nothing between. The rhomboids and middle trapezius a coach means are filed
  under *upper* back. Mapped `skos:broader` at **confidence `low`**, which by policy means it is
  never the sole basis for a safety decision.
- **No single "obliques" concept.** Modelled as a `skos:narrower` pair plus a broader parent, rather
  than picking one of the two muscles and calling it exact.
- **No unlateralised "Rotator cuff syndrome".** Only left (320021000119106) and right
  (320031000119109) variants exist, plus a broader parent. Rather than pick a side, the concept is
  anchored on `Disorder of rotator cuff` with both laterality variants as narrower. This is exactly
  the asymmetry that silently breaks a filter written assuming every condition has an unlateralised
  form.
- **No carpal tunnel body structure.** Only procedures. The carpal tunnel rule is therefore anchored
  on the wrist joint, making it slightly over-broad. Recorded in `conditions.json`.
- **"chest" is not pectoralis major.** Mapped `closeMatch`, not `exactMatch` — asserting identity
  would quietly drop pectoralis minor. NCIT's `Pectoralis_Muscle` is the better concept here, and is
  one of very few places the secondary vocabulary beats the primary.

### 5.3 OPE — pulled thinly, and the reason matters

**What OPE actually contains**, verified by enumerating all 634 classes through the BioPortal API and
grepping the downloaded OWL: **19 native classes and 615 imported NCI Thesaurus classes. Zero named
individuals.**

**What we pulled:**

- The **property vocabulary** as alignment targets for our edges: `ope:engages` (domain `Exercise`,
  range `NCIT:Musculoskeletal_System_Part`) for `TARGETS`, `ope:utilizesEquipment` for `REQUIRES`,
  `ope:isTreatmentFor` / `ope:isPreventativeFor` for the therapeutic direction of the rules.
- The **8 equipment categories** as `skos:broader` parents for all 32 equipment terms.
- The **4 contraction-mode and 4 intensity classes** (NCIT `Isometric_Exercise`, `Isotonic_Exercise`,
  `Aerobic_Exercise`, `Strenuous_Exercise` …) as broader parents for movement patterns.
- **NCIT muscle classes for 14 of the 19 muscle groups** as a secondary grounding beside SNOMED. The
  other five are declined: OPE has no group class for forearms, glutes, upper back or middle back,
  and no rotator cuff class at all (only one of the four cuff muscles is imported).
- The four **fitness-outcome** classes as `skos:related` targets — what a pattern trains.

**What we left out, and the honest caveat:** OPE contains **no concrete exercises and no concrete
equipment whatsoever**. There is no Dumbbell, Barbell, Kettlebell, bench, band, mat, or machine class
anywhere in it; `ConsumerEquipment` has no children. There is no squat, lunge, press, pull, hinge,
carry or jump. Its entire exercise taxonomy is eight classes covering contraction mode and intensity.

More importantly — and this must not be glossed — OPE's exercise↔ailment and exercise↔muscle
relations are **class-level existential axioms**, not curated content. The ontology asserts
`Ailment ⊑ ∃isTreatedBy.Exercise` ("every ailment is treated by *some* exercise") and
`Exercise ⊑ ∃engages.Musculoskeletal_System_Part`. No axiom anywhere says *which* exercise treats
*which* ailment, and with zero named individuals there is no instance data either.

**Therefore: every actual `TARGETS`, `STRESSES` and `CONTRAINDICATED_FOR` edge in this graph is our
own assertion.** OPE supplies the property vocabulary and the type signature that make those
assertions well-formed and citable. It does not supply the edges. Saying otherwise would overclaim
the grounding, so it is stated here plainly.

Also unused: `ope:RegulatedEquipment` (nothing in a gym catalog is regulated medical equipment),
`ope:WearablePersonalGearEquipment` and `ope:MonitorEquipment` (no wearables or sensors in the
catalog — these would become relevant if biomarker capture were modelled), and the bare motion
properties `flexes` / `extends` / `rotates` / `contracts` / `stretches`, which are declared with **no
domain or range** and so are citable as vocabulary but not usable as typed alignment targets.

### 5.4 COPPER — a narrow, deliberate slice

**What we pulled:** activity concepts where they match exactly — `copper:1017` Yoga, `copper:1041`
Stretching, `copper:1005` Resistance Training, `copper:1169` Cardio Training — plus barrier concepts
that align with clinical states: `copper:3040` back pain, `copper:3005` activity-induced pain,
`copper:3001` pain feeling.

**What we left out:** the whole behaviour-change apparatus — 51 coded BCTs, the weather and
transport barrier families, the household-activity taxonomy. This is genuinely rich material and
genuinely out of scope for KG 1. Its natural home is KG 2 (member context): `copper:5027`
`hasActivityAversion` is a much better model of Jordan's stated dislikes than anything here, and the
adherence and churn work would benefit from the barrier vocabulary. Flagged for whoever builds KG 2.

**COPPER's contraindication modelling is instructive as a counter-example.** COPPER *does* encode
contraindications — via a non-standard annotation property minted, improperly, in the `rdfs:`
namespace, carrying free-text strings on ~40 activity classes. `COPPER_1005` Resistance Training
reads `"Pain in back, legs, knee, feet; balance problems"`.

That is a prose warning attached to an entire training modality. It cannot distinguish a split squat
from a hip thrust, and it cannot be traversed. Our `contraindications.json` is the deliberate
inverse: rules that match on *mechanics* and resolve to a decision plus a mechanism. Seeing COPPER's
approach is part of why ours is shaped the way it is.

One practical caution: COPPER's native namespace is a `github.com/.../blob/main/` web page path, not
a resolvable identifier namespace. It will not content-negotiate to RDF and is treated as an opaque
string key.

### 5.5 The gap that matters most

**No published ontology in scope has a concept for plyometric or impact-loading exercise.**

This is not a minor omission. "Avoid plyometrics" is the explicit clinical instruction in the sample
member's injury note. It is the single most load-bearing safety concept in the entire dataset. And
OPE, COPPER, NCIT and SNOMED CT all lack it — OPE's taxonomy stops at contraction mode and intensity,
COPPER's activities are sport categories.

So `kgc:mp_cardio_plyometric` is minted locally, carries its own `impact: high` attribute, and is the
predicate two exclusion rules fire on. The concept the safety layer needs most is the one no ontology
supplies. If there is a single argument for why curating a small local vocabulary beats importing a
large foreign one wholesale, this is it.

### 5.6 Everything else minted locally

| Minted | Count | Why nothing published fit |
|---|--:|---|
| Movement patterns | 36 | No ontology models gym movement at pattern granularity |
| Equipment | 32 | OPE has categories only; no concrete implement exists anywhere |
| Mechanics attributes | 11 | The rule predicates. No published vocabulary carries impact, kinetic chain, or knee-flexion demand |
| Safety rules | 22 | COPPER's free-text strings are the only prior art, and are not traversable |
| PROV profile | 14 classes | A specialisation of PROV-O, not a replacement |

Policy: a term is minted in `kgc:` only *after* the left-out analysis records that no published
concept fits. Every minted concept in `data/ontology/` that has no mapping carries a `declined` block
naming the vocabularies checked and why each failed.

---

## 6. Contraindication rules

22 rules across 15 conditions: **10 exclude, 8 downrank, 2 allow, 2 promote**.

A rule matches on a condition's anatomy *plus* movement mechanics. It never names an exercise — so
adding an exercise to the catalog inherits the right safety behaviour with no rule edit.

### The mechanics vocabulary

11 attributes on each movement pattern, curated from the pattern's actual catalog usage:
`impact`, `kinetic_chain`, `external_load_typical`, `axial_spinal_load`, `knee_flexion_demand`,
`spinal_flexion_demand`, `spinal_rotation_demand`, `overhead_shoulder_demand`, `end_range_rom`,
`unilateral_lower_limb`, `is_therapeutic`.

`external_load_typical` is a pattern-level default, narrowed per exercise by the catalog's
`supports_weight` flag. Without that, a bodyweight jump squat would inherit "loaded" from the squat
pattern. `supports_weight` (28 true / 22 false) is one of the few catalog booleans carrying real
signal, and this is where it earns its keep.

### Worked example — the sample member

Patellofemoral pain, recovering, mild, left knee. Five rules apply:

| Rule | Decision | Matches on | Effect |
|---|---|---|---|
| `avoid_impact` | exclude | `impact: high`, **no anatomy gate** | Static Jump, Vertical Jump to Broad Jump, **Jumping Jack** |
| `avoid_loaded_deep_knee_flexion` | exclude | `knee_flexion_demand: deep` **AND** loaded **AND** closed/mixed chain, anatomy-gated | RNT Split Squat, DB Goblet Split Squat, Crossback Lunge |
| `downrank_moderate_knee_load` | downrank | `knee_flexion_demand: moderate` AND loaded | One-Kettlebell Hamstring Walkout |
| `allow_unloaded_therapeutic` | allow (priority 200) | `is_therapeutic` AND unloaded AND not moderate/high impact | **Rescues Cow Pose, World's Greatest Stretch** + 3 others |
| `promote_hip_abduction` | promote | hip abduction pattern or gluteus medius target | *No effect for this member* — see below |

Three details worth defending:

**The conjunction in rule 2 is deliberate.** Testing `knee_flexion_demand` alone would exclude the
quadruped position, where the shins bear the weight and the patellofemoral joint is unloaded.
Testing load alone would exclude a hip hinge. Only the conjunction describes the actual mechanism.

**Rule 1 waives the anatomy gate on purpose.** This is what catches Jumping Jack. The justification
is that landing forces travel the whole kinetic chain regardless of how the catalog tagged the row —
and the catalog's tagging is demonstrably incomplete.

**Down-rank, not exclude, for moderate loading.** The member completed box-supported goblet squats
pain-free on 2026-06-03 and said so in chat. Excluding that band outright would contradict her own
most recent evidence and her stated goal of returning to pain-free squatting.

### Same mechanics, different decisions

Deep loaded knee flexion is **excluded** for patellofemoral pain and merely **down-ranked** for knee
osteoarthritis — because progressive loading is the evidence-backed management for OA, and removing
it would be actively harmful advice. A system that cannot express that difference is a filter, not a
clinical model.

Similarly, `allow` rules are written defensively. `allow_unloaded_therapeutic` carries an explicit
`mechanics_none_of: {impact: [moderate, high]}` guard, because an allow rule outranks every
exclusion — without the guard, any future pattern flagged therapeutic but high-impact would be
rescued past the safety layer. Allow rules are the dangerous kind.

### An honest negative result

`promote_hip_abduction` has **no effect for this member**. Gluteus medius work is first-line
management for patellofemoral pain, so the rule should fire — but the catalog's only
`lower - abduction` exercise is the Alternating Dumbbell Racked Crossback Lunge, which is also a
loaded deep-flexion lunge and is therefore excluded by rule 2. The promote rule fires and loses on
priority, correctly.

The catalog simply contains no safe hip-abduction option for this member. That is a content gap, not
a schema bug, and the right response is to surface it to the coach rather than to weaken rule 2.

### Not modelled here

Preferences are **not** contraindications. Jordan's dislikes ("Deadlift", "Burpees") match zero
catalog exercise names — a literal exclusion is a no-op — so they resolve through `alt_labels` on
`mp_lower_pull_hip_lift` and `mp_cardio_plyometric` instead. But they stay in the preference layer.
Conflating "she does not like it" with "it is unsafe" would corrupt the audit trail.

---

## 7. Provenance (PROV-O)

[`prov-model.ttl`](../data/ontology/prov-model.ttl) is a PROV-O application profile — 14 classes,
each a subclass of `prov:Entity`, `prov:Activity` or `prov:Agent`. Nothing is invented where PROV-O
already has a term. [`prov-example.ttl`](../data/ontology/prov-example.ttl) is a full worked trace
for one real decision, using real catalog UUIDs.

The question it answers, for any line of a generated plan: **which rule fired, which ontology concept
it resolved through, and which member fact triggered it.**

```
CoachRequest ──used──▶ ConceptResolution ──generated──▶ ConceptMatch ──resolvedTo──▶ skos:Concept
                              │                              (resolverPass, confidence, sourceText)
                        wasInformedBy
                              ▼
MemberFact ──triggeredBy──▶ SafetyScreening ──generated──▶ Exclusion ──appliedRule──▶ SafetyRule
                              │        (agent: RuleEngine)      └──justifiedBy──▶ GraphPath
                        wasInformedBy
                              ▼
                        PlanAssembly ──generated──▶ ExercisePrescription ──▶ WorkoutPlan
```

Four modelling choices:

**`SafetyRule` is a `prov:Plan`.** Not a bespoke class — PROV-O's `prov:Plan` is exactly right, so a
screening activity cites its rule through `prov:qualifiedAssociation` / `prov:hadPlan`. The rule
*is* the plan the agent followed.

**Safety screening is attributed to `kgc:RuleEngine`, never to `kgc:LanguageModel`.** That
attribution split is the audit evidence that safety came from traversal rather than from a prompt
instruction. If a trace ever shows a `SafetyScreening` associated with the language model, that is a
bug visible in the data.

**`Exclusion` is a first-class entity.** Filtered-out exercises are recorded, not discarded, so the
trace shows what was removed and why — not only what survived. Coach overrides are recorded the same
way, attributed to the coach.

**`GraphPath` stores both the walked node sequence and the Cypher that produced it**, so a reviewer
can re-run the query. This is the artifact that makes "the graph decided" checkable rather than
asserted.

---

## 8. File format and build

All curated mappings are **JSON**; the PROV profile and its example are **Turtle**. The split is by
role, not by whim:

- **JSON is the curation format.** These files are hand-authored and hand-reviewed. Contraindication
  rules, mechanics attributes and confidence grades are not naturally SKOS triples, and JSON diffs
  legibly in review — which matters when a wrong edge is a safety failure.
- **Turtle is for the schema-level vocabulary.** `prov-model.ttl` declares classes and properties;
  that is a TBox, and Turtle is its native form.
- **RDF is derived, not hand-maintained.** A build script parses the JSON with `rdflib`, expands
  prefixes through `namespaces.json`, emits the SKOS and PROV-O triples, and loads Neo4j — per the
  decisions in [`tech-stack.md`](./tech-stack.md) and
  [`knowledge-graph-options.md`](./knowledge-graph-options.md).

Every IRI prefix used anywhere in `data/ontology/` is registered in `namespaces.json`. No file
hardcodes a full IRI.

### Integrity checks

The curation is machine-checkable, and the checks earned their keep — they caught four unreachable
conditions and one wrong hop count that manual review had missed:

- every catalog term in all four vocabularies is present, with no extras (32/32, 36/36, 19/19, 9/9)
- every `exercise_count` matches a recount of `data/exercises.json`
- every prefix used in a `target` is registered in `namespaces.json`
- every partonomy edge references a declared structure
- every condition anchor reaches a catalog joint under the documented closure, at the declared hop
  count and by the declared route
- every rule references a real condition, pattern and structure
- the catalog joints flagged in the partonomy match the exact-match targets in `joints.json`
- both Turtle files parse (161 and 125 triples)
- the `SUBSTITUTABLE_FOR` graph is referentially closed

---

## 9. Known gaps

Recorded here rather than left for a reviewer to find:

1. **Hip and thoracic spine have no conditions anchored to them.** Both are catalog joints — hip is
   tied for the most-loaded at 25 exercises. A hip complaint currently degrades to the blunt
   joint-level filter. Femoroacetabular impingement and thoracic facet irritation are the next two to
   curate.
2. **`middle back` is mapped at confidence `low`.** By policy it is display-and-traversal only.
3. **The carpal tunnel rule is over-broad**, anchored on the wrist joint because SNOMED has no
   structure for the tunnel.
4. **Lateral epicondylitis uses a proxy predicate.** The catalog has no grip-demand field, so
   open-chain loaded elbow work stands in. Stated in the rule rather than dressed up.
5. **No systemic or cardiometabolic conditions.** The lab panel is in scope for the copilot but not
   for the movement graph; modelling e.g. uncontrolled hypertension as a contraindication to heavy
   axial loading needs clinical sign-off this exercise cannot supply.
6. **Mechanics are pattern-level.** Only `external_load_typical` is refined per exercise. Exercise-level
   overrides for the other ten attributes would be the next accuracy improvement.
7. **`bilateral_pair_id` is unusable.** All 18 values are dangling and every unilateral exercise is
   `left_*`; the right-side counterparts were never shipped. Per-side programming has to synthesise
   the right side, which no amount of schema fixes.

---

## 10. Source verification

Every code and IRI in `data/ontology/` was fetched live, not recalled. Re-runnable:

```bash
# SNOMED CT — NCI EVS REST API, no auth required
curl -s "https://api-evsrest.nci.nih.gov/api/v1/concept/snomedct_us/49076000?include=summary,parents,children"
curl -s "https://api-evsrest.nci.nih.gov/api/v1/concept/snomedct_us/search?term=patellofemoral&pageSize=10"

# OPE and COPPER — BioPortal REST API
K=8b5b7825-538d-40e0-9e9e-5ab9274a9aeb   # public demo key
curl -s "https://data.bioontology.org/ontologies/OPE/classes?apikey=$K&pagesize=500" \
  | jq -r '.collection[] | "\(.prefLabel)\t\(.["@id"])"'
curl -s "https://data.bioontology.org/ontologies/COPPER/classes?apikey=$K&pagesize=500" \
  | jq -r '.collection[] | "\(.prefLabel)\t\(.["@id"])"'
```

| Source | Version | License |
|---|---|---|
| SNOMED CT US | `snomedct_us` 2025_09_01 via NCI EVS | Licensed; only identifiers and preferred terms stored |
| OPE | submission 6, 2013-03-13, v0.0.1 | None declared on the BioPortal submission |
| COPPER | submission 5, 2024-04-05, v2025-10-07 · [DOI](https://doi.org/10.1186/s12966-025-01744-5) | None declared on the BioPortal submission |
| PROV-O / SKOS | W3C Recommendations | W3C Document Licence |

Browse any SNOMED code at
`https://evsexplore.semantics.cancer.gov/evsexplore/concept/snomedct_us/{code}`.
