from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class RuleDecision(StrEnum):
    """What a fired safety rule does to a candidate exercise.

    Ordered most restrictive first; ``safety._DECISION_RANK`` depends on that
    order for its tie-break, and ``contraindications.json`` documents the same
    precedence under ``evaluation_semantics.resolution``.
    """

    EXCLUDE = "exclude"
    DOWNRANK = "downrank"
    PROMOTE = "promote"
    ALLOW = "allow"


class InjuryStatus(StrEnum):
    """How live an injury is, as recorded on the member's injury record.

    The vocabulary is the one contraindications.json writes ``applies_when``
    against. ``CHRONIC`` is part of it: four rules (knee OA, both low-back
    rules, carpal tunnel) list it, so omitting it here would make a chronic
    injury unrepresentable and those rules unreachable for such a member.
    """

    ACUTE = "acute"
    CHRONIC = "chronic"
    RECOVERING = "recovering"
    RESOLVED = "resolved"


class InjurySeverity(StrEnum):
    """How badly an injury presents, as recorded on the member's record."""

    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


class MechanicsAttr(StrEnum):
    """The movement-pattern mechanics a rule is allowed to match on.

    These are exactly the keys of ``mechanics`` in movement-patterns.json. A
    rule naming anything else is a curation error: it would match no pattern
    and the rule would silently never fire.
    """

    IMPACT = "impact"
    KINETIC_CHAIN = "kinetic_chain"
    EXTERNAL_LOAD_TYPICAL = "external_load_typical"
    AXIAL_SPINAL_LOAD = "axial_spinal_load"
    KNEE_FLEXION_DEMAND = "knee_flexion_demand"
    SPINAL_FLEXION_DEMAND = "spinal_flexion_demand"
    SPINAL_ROTATION_DEMAND = "spinal_rotation_demand"
    OVERHEAD_SHOULDER_DEMAND = "overhead_shoulder_demand"
    END_RANGE_ROM = "end_range_rom"
    UNILATERAL_LOWER_LIMB = "unilateral_lower_limb"
    IS_THERAPEUTIC = "is_therapeutic"


# Mechanics values are either graded strings ("impact": "high") or flags
# ("is_therapeutic": true). `bool` leads the union so Pydantic's smart mode
# keeps `true` a bool — as a string it would never equal the pattern's value
# and the clause would quietly stop matching.
MechanicsClause = dict[MechanicsAttr, list[bool | str]]


class AppliesWhen(BaseModel):
    """The injury states a rule is written for.

    A rule whose lists do not cover the member's recorded status/severity does
    not fire at all — this is the applicability gate, evaluated before any
    mechanics clause.
    """

    model_config = ConfigDict(extra="forbid")

    status: list[InjuryStatus]
    severity: list[InjurySeverity]


class ExerciseMatch(BaseModel):
    """Which exercises a rule fires on.

    The clause semantics are documented once in ``app.kg.safety``'s module
    docstring, because the evaluator and its tests both depend on them. In
    brief: ``all_of`` must be satisfied by a *single* movement pattern,
    ``any_of`` by any pattern, and ``none_of`` is an exercise-level veto.
    """

    model_config = ConfigDict(extra="forbid")

    require_anatomy_overlap: bool = Field(
        description="Gate the rule on the condition's anchor reaching one of "
        "the exercise's STRESSES joints through the PART_OF/IS_A closure."
    )
    mechanics_all_of: MechanicsClause | None = None
    mechanics_any_of: MechanicsClause | None = None
    mechanics_none_of: MechanicsClause | None = None
    pattern_id_any_of: list[str] = Field(default_factory=list)
    targets_any_of: list[str] = Field(
        default_factory=list,
        description="Anatomy CURIEs matched against the exercise's muscle and "
        "joint mappings.",
    )


class SafetyRule(BaseModel):
    """One curated contraindication rule, as authored in contraindications.json.

    ``extra="forbid"`` is load-bearing rather than tidiness: a misspelled key
    (``mechanics_any_off``) would otherwise be dropped in silence and the rule
    would never fire, which is the one failure mode a safety layer must not
    have. Every key the curated file uses is therefore declared here, including
    the prose fields the evaluator ignores.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    condition: str
    decision: RuleDecision
    priority: int
    applies_when: AppliesWhen
    exercise_match: ExerciseMatch
    rationale: str
    score_delta: float = 0.0
    escalate_to_exclude_when_acute: bool = False
    coach_overridable: bool = False

    # Curation notes. Unused at runtime and not stored on the graph node, but
    # declared so `extra="forbid"` can still catch a typo anywhere in the file.
    member_evidence: str | None = None
    anatomy_gate_waived_because: str | None = None
    why_three_predicates: str | None = None
    note: str | None = None
    rescues: list[str] | None = None


SAFETY_RULES_ADAPTER = TypeAdapter(list[SafetyRule])
