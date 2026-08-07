"""Tests for the safety/feasibility traversal (issue #12).

Why these paths are critical: this filter is the safety boundary the whole
system rests on — the LLM composes only from what it returns. The suite pins
the two documented failure directions of a naive ``joints_loaded`` filter
(docs/data-overview.md §3): it must KEEP harmless mobility work over the
injured joint (Cow Pose, World's Greatest Stretch) and must CATCH the
plyometric exercise whose catalog row omits the knee (Jumping Jack). It also
pins the rule-evaluation semantics the curated rules depend on, the
21-exercise equipment reference pool, explicit exclusions, and
alternative-finding. Pure Python + Cypher — no LLM anywhere.
"""

from __future__ import annotations

from typing import Any

from app.kg.rules import (
    AppliesWhen,
    ExerciseMatch,
    InjurySeverity,
    InjuryStatus,
    RuleDecision,
    SafetyRule,
)
from app.kg.safety import (
    InjuryConstraint,
    PoolConstraints,
    _evaluate_rules,
    _winner,
    safe_exercise_pool,
)

JORDAN_EQUIPMENT = [
    "eq_dumbbell",
    "eq_kettlebell",
    "eq_resistance_band_loop",
    "eq_flat_bench",
    "eq_yoga_mat",
]
PFPS = InjuryConstraint(
    condition_id="cond_pfps",
    status=InjuryStatus.RECOVERING,
    severity=InjurySeverity.MILD,
)


# --------------------------------------------------------------------------
# Unit layer: rule-evaluation semantics on synthetic rows
# --------------------------------------------------------------------------


def _row(
    patterns: list[dict[str, Any]],
    joints: list[str] | None = None,
    supports_weight: bool = True,
) -> dict[str, Any]:
    joints = joints if joints is not None else ["jt_knee"]
    return {
        "exercise": {
            "id": "ex_test",
            "name": "Test Exercise",
            "supports_weight": supports_weight,
            "stresses_recorded": bool(joints),
        },
        "patterns": patterns,
        "joints": [{"id": j, "label": j, "curie": f"snomed:{j}"} for j in joints],
        "equipment": [],
        "muscles": [],
    }


_ANY_SEVERITY = [InjurySeverity.MILD, InjurySeverity.MODERATE, InjurySeverity.SEVERE]


def _rule(
    decision: RuleDecision = RuleDecision.EXCLUDE,
    priority: int = 100,
    match: dict[str, Any] | None = None,
    *,
    score_delta: float | None = None,
    applies_when: AppliesWhen | None = None,
    escalate_to_exclude_when_acute: bool = False,
) -> SafetyRule:
    """Build a synthetic rule, defaulting everything the test does not pin."""
    if score_delta is None:
        score_delta = -0.35 if decision is RuleDecision.DOWNRANK else 0.0
    return SafetyRule(
        id=f"rule_test_{decision}",
        condition="cond_pfps",
        decision=decision,
        priority=priority,
        rationale="test rule",
        score_delta=score_delta,
        applies_when=applies_when
        or AppliesWhen(
            status=[InjuryStatus.ACUTE, InjuryStatus.RECOVERING],
            severity=_ANY_SEVERITY,
        ),
        # Merged as a dict rather than kwargs so a test may override the gate.
        exercise_match=ExerciseMatch.model_validate(
            {"require_anatomy_overlap": False, **(match or {})}
        ),
        escalate_to_exclude_when_acute=escalate_to_exclude_when_acute,
    )


CLOSURE = {"cond_pfps": {"jt_knee": None}}  # anchor reaches the knee


def _fire(row, rule, injury=PFPS, closures=CLOSURE):
    return _evaluate_rules(row, [injury], {"cond_pfps": [rule]}, closures)


def test_all_of_is_a_single_pattern_conjunction() -> None:
    # Guards: the loaded-deep-knee-flexion rule firing on an exercise whose
    # attributes are spread over DIFFERENT patterns — e.g. a deep-flexion
    # mobility pattern plus a separate loaded pattern would wrongly ban a
    # harmless combination if the conjunction weren't per-pattern.
    rule = _rule(match={"mechanics_all_of": {
        "knee_flexion_demand": ["deep"], "external_load_typical": [True]}})
    split = _row([
        {"id": "p1", "knee_flexion_demand": "deep", "external_load_typical": False},
        {"id": "p2", "knee_flexion_demand": "none", "external_load_typical": True},
    ])
    assert _fire(split, rule) == []

    together = _row([
        {"id": "p1", "knee_flexion_demand": "deep", "external_load_typical": True},
    ])
    assert len(_fire(together, rule)) == 1


def test_none_of_vetoes_at_exercise_level() -> None:
    # Guards: the allow rule rescuing a therapeutic-but-high-impact exercise
    # past the safety layer — the guard must look at ALL patterns, not just
    # the one that satisfied all_of.
    rule = _rule(
        decision=RuleDecision.ALLOW,
        priority=200,
        match={
            "mechanics_all_of": {"is_therapeutic": [True],
                                 "external_load_typical": [False]},
            "mechanics_none_of": {"impact": ["moderate", "high"]},
        },
    )
    therapeutic_but_plyo = _row(
        [
            {"id": "p1", "is_therapeutic": True, "external_load_typical": False,
             "impact": "none"},
            {"id": "p2", "is_therapeutic": False, "external_load_typical": False,
             "impact": "high"},
        ],
        supports_weight=False,
    )
    assert _fire(therapeutic_but_plyo, rule) == []


def test_external_load_narrowed_by_supports_weight() -> None:
    # Guards: a bodyweight exercise inheriting "loaded" from its pattern
    # default — the catalog's supports_weight flag must narrow it, or a
    # bodyweight squat pattern would be banned as if it were barbell-loaded.
    rule = _rule(match={"mechanics_all_of": {"external_load_typical": [True]}})
    pattern = {"id": "p1", "external_load_typical": True}
    assert len(_fire(_row([pattern], supports_weight=True), rule)) == 1
    assert _fire(_row([pattern], supports_weight=False), rule) == []


def test_anatomy_gate_and_unknown_joints() -> None:
    # Guards both directions of the gate: a rule for a knee condition firing
    # on an exercise that never stresses the knee, and — quirk 4 — an
    # exercise with EMPTY joints_loaded satisfying an anatomy-gated rule
    # (missing anatomy means unknown, not safe; only mechanics-only rules
    # may speak for those rows).
    gated = _rule(match={"require_anatomy_overlap": True})
    stresses_elbow = _row([{"id": "p1"}], joints=["jt_elbow"])
    assert _fire(stresses_elbow, gated) == []

    no_joints = _row([{"id": "p1"}], joints=[])
    assert _fire(no_joints, gated) == []

    ungated = _rule(match={"require_anatomy_overlap": False})
    assert len(_fire(no_joints, ungated)) == 1


def test_applies_when_filters_and_escalation() -> None:
    # Guards: a rule scoped to acute/recovering firing on a resolved injury,
    # and the acute escalation (downrank -> exclude) being lost — which would
    # let moderate knee loading through during an acute flare.
    rule = _rule(
        decision=RuleDecision.DOWNRANK,
        priority=50,
        applies_when=AppliesWhen(
            status=[InjuryStatus.ACUTE, InjuryStatus.RECOVERING],
            severity=[InjurySeverity.MILD],
        ),
        escalate_to_exclude_when_acute=True,
    )
    row = _row([{"id": "p1"}])
    resolved = InjuryConstraint(
        condition_id="cond_pfps",
        status=InjuryStatus.RESOLVED,
        severity=InjurySeverity.MILD,
    )
    assert _fire(row, rule, injury=resolved) == []

    acute = InjuryConstraint(
        condition_id="cond_pfps",
        status=InjuryStatus.ACUTE,
        severity=InjurySeverity.MILD,
    )
    firings = _fire(row, rule, injury=acute)
    assert firings[0].decision is RuleDecision.EXCLUDE
    assert firings[0].escalated_from is RuleDecision.DOWNRANK


def test_winner_priority_then_restrictiveness() -> None:
    # Guards: the allow-beats-exclude priority band inverting, or a tie
    # resolving permissively — either would weaken the safety decision.
    row = _row([{"id": "p1"}])
    exclude = _rule(decision=RuleDecision.EXCLUDE, priority=100)
    allow = _rule(decision=RuleDecision.ALLOW, priority=200)
    downrank = _rule(decision=RuleDecision.DOWNRANK, priority=100, score_delta=-0.2)

    firings = _evaluate_rules(
        row, [PFPS], {"cond_pfps": [exclude, allow]}, CLOSURE
    )
    assert _winner(firings).decision is RuleDecision.ALLOW

    tied = _evaluate_rules(
        row, [PFPS], {"cond_pfps": [exclude, downrank]}, CLOSURE
    )
    assert _winner(tied).decision is RuleDecision.EXCLUDE


# --------------------------------------------------------------------------
# Integration layer: the reference member scenario on the real graph
# --------------------------------------------------------------------------


async def test_equipment_pool_matches_reference(graph_driver) -> None:
    # Guards: the feasibility subset test drifting from the documented
    # 21-exercise pool for Jordan's five home items (docs/data-overview.md §3)
    # — the number every safety claim in the docs is calibrated against.
    result = await safe_exercise_pool(
        graph_driver, PoolConstraints(equipment_ids=JORDAN_EQUIPMENT)
    )
    assert len(result.included) == 21
    names = {e.name for e in result.included}
    assert "Cow Pose" in names and "Jumping Jack" in names  # nothing safety-filtered yet


async def test_knee_case_filters_both_directions(graph_driver) -> None:
    # Guards THE core failure of a joints_loaded filter, in both directions:
    # mobility work over the recovering knee must survive (false positives
    # rescued by the allow rule) while Jumping Jack — plyometric, know-nothing
    # joints_loaded — must be excluded by the anatomy-waived impact rule.
    result = await safe_exercise_pool(
        graph_driver,
        PoolConstraints(equipment_ids=JORDAN_EQUIPMENT, injuries=[PFPS]),
    )
    included = {e.name for e in result.included}
    excluded = {e.name: e for e in result.excluded if e.kind == "safety"}

    assert "Cow Pose" in included
    assert "World's Greatest Stretch" in included
    assert "Jumping Jack" in excluded
    assert "rule_pfps_avoid_impact" in excluded["Jumping Jack"].reason

    # The full documented exclusion set: 3 plyometric + 3 loaded deep flexion.
    assert set(excluded) == {
        "Jumping Jack",
        "Static Jump",
        "Vertical Jump to Broad Jump",
        "RNT Split Squat",
        "Dumbbell Goblet Split Squat",
        "Alternating Dumbbell Racked Crossback Lunge",
    }
    assert len(result.included) == 15

    # Loaded deep-knee-flexion exclusions must carry the anatomy path for
    # the provenance trace (patellofemoral joint -> knee).
    split_squat = excluded["Dumbbell Goblet Split Squat"]
    assert split_squat.path is not None
    assert "patellofemoral" in split_squat.path.description.lower()


async def test_explicit_exclusion_by_pattern(graph_driver) -> None:
    # Guards: "exclude deadlifts" resolving but then failing to remove the
    # hip-hinge exercises — the coach's explicit instruction must always win.
    result = await safe_exercise_pool(
        graph_driver,
        PoolConstraints(exclude_concept_ids=["mp_lower_pull_hip_lift"]),
    )
    excluded = {e.name for e in result.excluded if e.kind == "explicit"}
    assert excluded == {
        "One-Kettlebell Hamstring Walkout",
        "Med Ball Hamstring Walkout",
    }


async def test_preference_downranks_but_keeps(graph_driver) -> None:
    # Guards: dislikes being treated as safety exclusions (corrupting the
    # audit trail) or being dropped entirely — they must keep the exercise
    # in the pool with a negative score and a preference-labelled note.
    result = await safe_exercise_pool(
        graph_driver,
        PoolConstraints(
            equipment_ids=JORDAN_EQUIPMENT,
            downrank_concept_ids=["mp_lower_pull_hip_lift"],
        ),
    )
    walkout = next(
        e for e in result.included if e.name == "One-Kettlebell Hamstring Walkout"
    )
    assert walkout.score < 0
    assert any("preference" in note for note in walkout.notes)


async def test_alternatives_for_equipment_exclusions(graph_driver) -> None:
    # Guards: the substitution feature returning nothing (or unsafe picks)
    # when equipment rules out an exercise — alternatives must come from the
    # surviving pool and share a movement pattern or muscle target.
    result = await safe_exercise_pool(
        graph_driver,
        PoolConstraints(equipment_ids=JORDAN_EQUIPMENT, injuries=[PFPS]),
    )
    included_ids = {e.exercise_id for e in result.included}
    bench_press = next(
        e for e in result.excluded if e.name == "Barbell Decline Bench Press"
    )
    assert bench_press.kind == "equipment"
    assert bench_press.alternatives
    for alternative in bench_press.alternatives:
        assert alternative.exercise_id in included_ids
        assert alternative.shared_patterns or alternative.shared_muscle_groups


async def test_unknown_condition_is_surfaced_not_silent(graph_driver) -> None:
    # Guards: an injury whose condition has no rules being silently
    # unfiltered — the coach must be told the graph cannot vouch for it.
    result = await safe_exercise_pool(
        graph_driver,
        PoolConstraints(
            injuries=[InjuryConstraint(condition_id="cond_does_not_exist")]
        ),
    )
    assert any("cond_does_not_exist" in note for note in result.notes)
