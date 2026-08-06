"""``safe_exercise_pool`` — the deterministic safety/feasibility filter (issue #6).

Pure Python + Cypher, no LLM anywhere: this is the layer that keeps safety out
of the prompt. The generator's LLM composes workouts from the pool this
function returns and can never introduce an exercise it filtered out.

Filter order, each stage recorded in the result rather than silently applied:

1. **Explicit exclusions** — already-resolved concept IDs (an exercise, a
   movement pattern, an equipment item, a muscle group). "Exclude deadlifts"
   arrives here as ``mp_lower_pull_hip_lift`` from the resolver.
2. **Equipment feasibility** — ``REQUIRES ⊆ available`` subset test. For the
   sample member's five home items this yields the reference 21-exercise pool
   (docs/data-overview.md §3).
3. **Safety rules** — every rule hanging off each injury's condition is
   evaluated for every candidate; the fired set is kept in full so provenance
   can show every reason, not just the winner (contraindications.json
   ``evaluation_semantics``). Winner = highest priority; ties go to the more
   restrictive decision (exclude > downrank > promote > allow).
4. **Preference down-ranking** — dislikes resolved to concept IDs push the
   score down; they are preferences, never safety, and are labelled as such.

Rule-evaluation semantics (documented once, here, because tests depend on it):

- ``mechanics_all_of`` must be satisfied by a **single** movement pattern of
  the exercise — the conjunction describes one mechanism (a loaded deep-knee
  -flexion *pattern*), not attributes scattered across patterns.
- ``mechanics_any_of`` fires if **any** pattern matches any listed value.
- ``mechanics_none_of`` is an exercise-level veto: **no** pattern may match.
  This is the guard that keeps an allow rule from rescuing a hypothetical
  therapeutic-but-high-impact pattern past the safety layer.
- ``external_load_typical`` is a pattern-level default narrowed by the
  exercise's ``supports_weight`` flag: the effective value is
  ``pattern.external_load_typical AND exercise.supports_weight``.
- ``require_anatomy_overlap: true`` gates the rule on the condition's
  ``ANCHORED_AT`` anchor reaching one of the exercise's ``STRESSES`` joints
  through the ``PART_OF | IS_A`` closure (depth ≤ 5, one optional step back
  down — docs/kg1-schema.md §4). Exercises whose ``joints_loaded`` was empty
  in the catalog (``stresses_recorded: false``) can never satisfy the gate:
  missing anatomy means *unknown*, not safe, and only mechanics-only rules
  can vouch either way for those rows.
- Rules whose ``applies_when`` does not cover the injury's status/severity do
  not fire; ``escalate_to_exclude_when_acute`` upgrades a downrank to an
  exclusion when the injury is acute.

Every inclusion and every safety exclusion carries a ``GraphPath`` — the
walked node sequence plus the Cypher that reproduces it — so the PROV-O trace
can show *which rule fired, through which anatomy, from which member fact*.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from neo4j import AsyncDriver
from pydantic import BaseModel, Field

# Preferences push an exercise down the ranking without touching safety.
PREFERENCE_SCORE_DELTA = -0.3
MAX_ALTERNATIVES = 3

# Tie-break order when two fired rules share a priority: more restrictive wins.
_DECISION_RANK = {"exclude": 0, "downrank": 1, "promote": 2, "allow": 3}

CLOSURE_CYPHER = (
    "MATCH (c:Condition {id: $condition_id})-[:ANCHORED_AT]->(anchor) "
    "MATCH p = (anchor)-[:PART_OF|IS_A*0..5]->(anc:AnatomicalStructure) "
    "MATCH (j:Joint) WHERE j = anc OR (j)-[:PART_OF]->(anc) "
    "RETURN j, p"
)


class InjuryConstraint(BaseModel):
    """One active injury, already resolved to a Condition concept."""

    condition_id: str
    status: Literal["acute", "recovering", "resolved"] = "recovering"
    severity: Literal["mild", "moderate", "severe"] = "mild"


class PoolConstraints(BaseModel):
    """Input to ``safe_exercise_pool`` — concept IDs only, resolution happens upstream."""

    equipment_ids: list[str] | None = None  # None = equipment-unconstrained
    injuries: list[InjuryConstraint] = Field(default_factory=list)
    exclude_concept_ids: list[str] = Field(default_factory=list)
    downrank_concept_ids: list[str] = Field(default_factory=list)


class GraphPath(BaseModel):
    """A walked path plus the query that reproduces it (PROV-O ``GraphPath``)."""

    nodes: list[str]
    description: str
    cypher: str


class RuleFiring(BaseModel):
    """One safety rule that fired for one exercise."""

    rule_id: str
    condition_id: str
    decision: Literal["exclude", "downrank", "promote", "allow"]
    priority: int
    score_delta: float = 0.0
    rationale: str
    escalated_from: str | None = None
    anatomy_path: GraphPath | None = None


class Alternative(BaseModel):
    """A feasible, safe substitute for an excluded exercise."""

    exercise_id: str
    name: str
    shared_patterns: list[str]
    shared_muscle_groups: list[str]


class PoolExercise(BaseModel):
    """An exercise that survived every filter, with its justification."""

    exercise_id: str
    name: str
    score: float = 0.0
    supports_weight: bool
    estimated_rep_duration: float
    is_reps: bool
    muscle_groups: list[str]
    movement_patterns: list[str]
    equipment: list[str]
    inclusion_path: GraphPath
    fired_rules: list[RuleFiring] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ExcludedExercise(BaseModel):
    """An exercise removed by a filter, with why and what to do instead."""

    exercise_id: str
    name: str
    kind: Literal["explicit", "equipment", "safety"]
    reason: str
    missing_equipment: list[str] = Field(default_factory=list)
    fired_rules: list[RuleFiring] = Field(default_factory=list)
    path: GraphPath | None = None
    alternatives: list[Alternative] = Field(default_factory=list)


class PoolResult(BaseModel):
    """The filtered pool plus the full account of what was removed and why."""

    included: list[PoolExercise]
    excluded: list[ExcludedExercise]
    notes: list[str] = Field(default_factory=list)


async def _fetch_catalog(driver: AsyncDriver) -> list[dict[str, Any]]:
    # The muscle 'curies' comprehension keeps only identity mappings: "glutes"
    # skos:narrower "gluteus medius" must not make every glute exercise count
    # as targeting gluteus medius (the promote rule would over-fire).
    records, _, _ = await driver.execute_query(
        "MATCH (e:Exercise) RETURN e{.*} AS exercise, "
        "[(e)-[:HAS_PATTERN]->(p:MovementPattern) | p{.*}] AS patterns, "
        "[(e)-[:STRESSES]->(j:Joint) | "
        "{id: j.id, label: j.pref_label, curie: j.curie}] AS joints, "
        "[(e)-[:REQUIRES]->(q:Equipment) | "
        "{id: q.id, label: q.pref_label}] AS equipment, "
        "[(e)-[:TARGETS]->(m:MuscleGroup) | {id: m.id, label: m.pref_label, "
        "curies: [(m)-[:MAPS_TO {predicate: 'exact_match'}]->"
        "(s:AnatomicalStructure) | s.curie]}] AS muscles",
        routing_="r",
    )
    return [dict(record) for record in records]


async def _fetch_rules(
    driver: AsyncDriver, condition_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    records, _, _ = await driver.execute_query(
        "MATCH (r:SafetyRule)-[:CONTRAINDICATED_FOR]->(c:Condition) "
        "WHERE c.id IN $ids RETURN c.id AS condition_id, r{.*} AS rule",
        ids=condition_ids,
    )
    rules: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        rule = dict(record["rule"])
        rule["applies_when"] = json.loads(rule.pop("applies_when_json"))
        rule["exercise_match"] = json.loads(rule.pop("exercise_match_json"))
        rules.setdefault(record["condition_id"], []).append(rule)
    return rules


async def _fetch_closures(
    driver: AsyncDriver, condition_ids: list[str]
) -> dict[str, dict[str, GraphPath]]:
    """Per condition: catalog joints reachable under the anatomy closure.

    Returns ``{condition_id: {joint_id: GraphPath}}`` where the path is the
    shortest walk from the condition's anchor to that joint, serialized for
    the provenance trace.
    """
    closures: dict[str, dict[str, GraphPath]] = {}
    for condition_id in condition_ids:
        records, _, _ = await driver.execute_query(
            "MATCH (c:Condition {id: $condition_id})-[:ANCHORED_AT]->(anchor) "
            "MATCH p = (anchor)-[:PART_OF|IS_A*0..5]->(anc:AnatomicalStructure) "
            "MATCH (j:Joint) WHERE j = anc OR (j)-[:PART_OF]->(anc) "
            "WITH j, anc, p ORDER BY length(p) "
            "WITH j, head(collect({names: [n IN nodes(p) | "
            "coalesce(n.pref_label, n.label)], anc_curie: anc.curie})) AS best "
            "RETURN j.id AS joint_id, j.pref_label AS joint_label, "
            "j.curie AS joint_curie, best",
            condition_id=condition_id,
        )
        per_joint: dict[str, GraphPath] = {}
        for record in records:
            names: list[str] = list(record["best"]["names"])
            if record["best"]["anc_curie"] != record["joint_curie"]:
                # The documented single optional step back down (§4).
                names.append(f"{record['joint_label']} (one PART_OF step down)")
            per_joint[record["joint_id"]] = GraphPath(
                nodes=names,
                description=(
                    f"{condition_id} anchor "
                    + " → ".join(names)
                    + " [catalog joint]"
                ),
                cypher=CLOSURE_CYPHER.replace("$condition_id", f"'{condition_id}'"),
            )
        closures[condition_id] = per_joint
    return closures


def _effective(pattern: dict[str, Any], attr: str, exercise: dict[str, Any]) -> Any:
    if attr == "external_load_typical":
        return bool(pattern.get(attr)) and bool(exercise.get("supports_weight"))
    return pattern.get(attr)


def _mechanics_fire(
    match: dict[str, Any],
    patterns: list[dict[str, Any]],
    exercise: dict[str, Any],
    exercise_curies: set[str],
) -> bool:
    """Positive clauses of ``exercise_match`` (veto and gate handled by caller)."""
    clauses: list[bool] = []
    if all_of := match.get("mechanics_all_of"):
        clauses.append(
            any(
                all(
                    _effective(p, attr, exercise) in allowed
                    for attr, allowed in all_of.items()
                )
                for p in patterns
            )
        )
    if any_of := match.get("mechanics_any_of"):
        clauses.append(
            any(
                _effective(p, attr, exercise) in allowed
                for p in patterns
                for attr, allowed in any_of.items()
            )
        )
    if pattern_ids := match.get("pattern_id_any_of"):
        clauses.append(any(p["id"] in pattern_ids for p in patterns))
    if targets := match.get("targets_any_of"):
        clauses.append(bool(exercise_curies & set(targets)))
    # A rule with no positive clause (anatomy-gate only) applies to everything
    # its gate admits.
    return any(clauses) if clauses else True


def _none_of_veto(
    match: dict[str, Any], patterns: list[dict[str, Any]], exercise: dict[str, Any]
) -> bool:
    none_of = match.get("mechanics_none_of")
    if not none_of:
        return False
    return any(
        _effective(p, attr, exercise) in banned
        for p in patterns
        for attr, banned in none_of.items()
    )


def _evaluate_rules(
    row: dict[str, Any],
    injuries: list[InjuryConstraint],
    rules_by_condition: dict[str, list[dict[str, Any]]],
    closures: dict[str, dict[str, GraphPath]],
) -> list[RuleFiring]:
    exercise = row["exercise"]
    patterns = row["patterns"]
    stressed_joint_ids = {j["id"] for j in row["joints"] if j.get("id")}
    exercise_curies = {
        curie for m in row["muscles"] for curie in (m.get("curies") or [])
    } | {j["curie"] for j in row["joints"] if j.get("curie")}

    firings: list[RuleFiring] = []
    for injury in injuries:
        for rule in rules_by_condition.get(injury.condition_id, []):
            applies = rule["applies_when"]
            if (
                injury.status not in applies.get("status", [])
                or injury.severity not in applies.get("severity", [])
            ):
                continue
            match = rule["exercise_match"]
            anatomy_path: GraphPath | None = None
            if match.get("require_anatomy_overlap"):
                reachable = closures.get(injury.condition_id, {})
                overlap = stressed_joint_ids & set(reachable)
                if not overlap:
                    continue
                anatomy_path = reachable[sorted(overlap)[0]]
            if _none_of_veto(match, patterns, exercise):
                continue
            if not _mechanics_fire(match, patterns, exercise, exercise_curies):
                continue
            decision = rule["decision"]
            priority = rule["priority"]
            escalated_from: str | None = None
            if (
                rule.get("escalate_to_exclude_when_acute")
                and injury.status == "acute"
                and decision != "exclude"
            ):
                escalated_from = decision
                decision, priority = "exclude", max(priority, 100)
            firings.append(
                RuleFiring(
                    rule_id=rule["id"],
                    condition_id=injury.condition_id,
                    decision=decision,
                    priority=priority,
                    score_delta=rule.get("score_delta") or 0.0,
                    rationale=rule["rationale"],
                    escalated_from=escalated_from,
                    anatomy_path=anatomy_path,
                )
            )
    return firings


def _winner(firings: list[RuleFiring]) -> RuleFiring | None:
    if not firings:
        return None
    return min(firings, key=lambda f: (-f.priority, _DECISION_RANK[f.decision]))


def _explicit_hit(row: dict[str, Any], concept_ids: set[str]) -> list[str]:
    """Concept IDs from the set that this exercise matches, for the record."""
    hits = [row["exercise"]["id"]] if row["exercise"]["id"] in concept_ids else []
    for group, key in (("patterns", "id"), ("joints", "id")):
        hits.extend(x[key] for x in row[group] if x.get(key) in concept_ids)
    hits.extend(q["id"] for q in row["equipment"] if q.get("id") in concept_ids)
    hits.extend(m["id"] for m in row["muscles"] if m.get("id") in concept_ids)
    return sorted(set(hits))


def _alternatives(
    excluded_row: dict[str, Any], included: list[PoolExercise]
) -> list[Alternative]:
    my_patterns = {p["id"]: p["pref_label"] for p in excluded_row["patterns"]}
    my_muscles = {m["id"]: m["label"] for m in excluded_row["muscles"]}
    candidates = []
    for pooled in included:
        shared_p = sorted(set(pooled.movement_patterns) & set(my_patterns.values()))
        shared_m = sorted(set(pooled.muscle_groups) & set(my_muscles.values()))
        if shared_p or shared_m:
            candidates.append(
                Alternative(
                    exercise_id=pooled.exercise_id,
                    name=pooled.name,
                    shared_patterns=shared_p,
                    shared_muscle_groups=shared_m,
                )
            )
    candidates.sort(
        key=lambda a: (-len(a.shared_patterns), -len(a.shared_muscle_groups), a.name)
    )
    return candidates[:MAX_ALTERNATIVES]


async def safe_exercise_pool(
    driver: AsyncDriver, constraints: PoolConstraints
) -> PoolResult:
    """Filter the catalog through the graph; return the pool plus provenance.

    Args:
        driver: Injected Neo4j async driver.
        constraints: Already-resolved concept IDs — this function never sees
            free text; ``resolve_concepts`` runs upstream.

    Returns:
        PoolResult whose ``included`` list is sorted best-first and whose
        ``excluded`` list accounts for every removed exercise.
    """
    catalog = await _fetch_catalog(driver)
    condition_ids = [i.condition_id for i in constraints.injuries]
    rules_by_condition = await _fetch_rules(driver, condition_ids)
    closures = await _fetch_closures(driver, condition_ids)
    notes: list[str] = []
    for injury in constraints.injuries:
        if injury.condition_id not in rules_by_condition:
            notes.append(
                f"no safety rules found for condition {injury.condition_id!r}; "
                "its injuries are NOT being filtered — surface this to the coach"
            )

    exclude_ids = set(constraints.exclude_concept_ids)
    downrank_ids = set(constraints.downrank_concept_ids)
    available = (
        set(constraints.equipment_ids)
        if constraints.equipment_ids is not None
        else None
    )

    included: list[PoolExercise] = []
    excluded: list[ExcludedExercise] = []
    equipment_excluded_rows: list[dict[str, Any]] = []

    for row in catalog:
        exercise = row["exercise"]
        name = exercise["name"]

        hits = _explicit_hit(row, exclude_ids) if exclude_ids else []
        if hits:
            excluded.append(
                ExcludedExercise(
                    exercise_id=exercise["id"],
                    name=name,
                    kind="explicit",
                    reason=f"matches explicitly excluded concept(s): {', '.join(hits)}",
                )
            )
            continue

        required = {q["id"]: q["label"] for q in row["equipment"] if q.get("id")}
        if available is not None:
            missing = sorted(
                label for qid, label in required.items() if qid not in available
            )
            if missing:
                excluded.append(
                    ExcludedExercise(
                        exercise_id=exercise["id"],
                        name=name,
                        kind="equipment",
                        reason=f"requires unavailable equipment: {', '.join(missing)}",
                        missing_equipment=missing,
                        path=GraphPath(
                            nodes=[name, *missing],
                            description=(
                                f"'{name}' REQUIRES {sorted(required.values())}; "
                                f"not a subset of available equipment"
                            ),
                            cypher=(
                                "MATCH (e:Exercise {id: '"
                                + exercise["id"]
                                + "'})-[:REQUIRES]->(q:Equipment) "
                                "RETURN q.id, q.pref_label"
                            ),
                        ),
                    )
                )
                equipment_excluded_rows.append(row)
                continue

        firings = _evaluate_rules(
            row, constraints.injuries, rules_by_condition, closures
        )
        winner = _winner(firings)
        if winner is not None and winner.decision == "exclude":
            excluded.append(
                ExcludedExercise(
                    exercise_id=exercise["id"],
                    name=name,
                    kind="safety",
                    reason=(
                        f"rule {winner.rule_id} ({winner.rationale.split('.')[0]})"
                    ),
                    fired_rules=firings,
                    path=winner.anatomy_path,
                )
            )
            continue

        score = sum(
            f.score_delta for f in firings if f.decision in ("downrank", "promote")
        )
        exercise_notes = [
            f"{f.decision} by {f.rule_id} (Δ{f.score_delta:+.2f})"
            for f in firings
            if f.decision in ("downrank", "promote")
        ]
        if winner is not None and winner.decision == "allow":
            exercise_notes.append(
                f"explicitly allowed by {winner.rule_id}: {winner.rationale}"
            )
        if not exercise["stresses_recorded"]:
            exercise_notes.append(
                "catalog recorded no loaded joints for this exercise; "
                "anatomy-gated safety rules could not evaluate it"
            )
        pref_hits = _explicit_hit(row, downrank_ids) if downrank_ids else []
        if pref_hits:
            score += PREFERENCE_SCORE_DELTA
            exercise_notes.append(
                "down-ranked by member preference "
                f"({', '.join(pref_hits)}, Δ{PREFERENCE_SCORE_DELTA:+.2f}) — "
                "a preference, not a safety exclusion"
            )

        equipment_desc = (
            f"REQUIRES {sorted(required.values())} ⊆ available"
            if available is not None and required
            else "no equipment required"
            if not required
            else f"REQUIRES {sorted(required.values())} (equipment unconstrained)"
        )
        included.append(
            PoolExercise(
                exercise_id=exercise["id"],
                name=name,
                score=round(score, 4),
                supports_weight=exercise["supports_weight"],
                estimated_rep_duration=exercise["estimated_rep_duration"],
                is_reps=exercise["is_reps"],
                muscle_groups=sorted(m["label"] for m in row["muscles"]),
                movement_patterns=sorted(p["pref_label"] for p in row["patterns"]),
                equipment=sorted(required.values()),
                inclusion_path=GraphPath(
                    nodes=[name],
                    description=f"'{name}': {equipment_desc}; "
                    + (
                        f"survived {len(firings)} fired rule(s)"
                        if firings
                        else "no safety rule fired"
                    ),
                    cypher=(
                        "MATCH (e:Exercise {id: '" + exercise["id"] + "'}) "
                        "OPTIONAL MATCH (e)-[:REQUIRES]->(q) RETURN e, collect(q)"
                    ),
                ),
                fired_rules=firings,
                notes=exercise_notes,
            )
        )

    included.sort(key=lambda e: (-e.score, e.name))
    for excluded_entry, row in zip(
        (e for e in excluded if e.kind == "equipment"), equipment_excluded_rows
    ):
        excluded_entry.alternatives = _alternatives(row, included)

    return PoolResult(included=included, excluded=excluded, notes=notes)
