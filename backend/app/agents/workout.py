"""Workout generator agent (surface A, issue #8).

The runtime is a **shallow, deterministic loop** — resolve, fetch pool,
generate — with the LLM used exactly twice and never in charge of safety:

1. *Extract* (LLM): pull constraint mentions out of the coach's free-text
   message into a typed ``ConstraintMentions``.
2. *Resolve* (no LLM): map each mention onto graph concepts with
   ``resolve_concepts`` and merge them into the accumulated ``ConstraintSet``
   (member defaults + prior turns + this message).
3. *Traverse* (no LLM): ``safe_exercise_pool`` filters the catalog through
   the graph and returns the safe pool plus provenance.
4. *Compose* (LLM): the model arranges exercises **from the pool it was
   given** into warmup/main/cooldown with sets/reps/rest. An output validator
   rejects any plan that references an exercise outside the pool or blows the
   time window, so the LLM cannot re-introduce a filtered exercise.

Member defaults (equipment, injuries, dislikes) load from KG 2 and are
auto-applied on every request. Adjustments work by sending the previous
response's ``constraints_used`` back as ``prior_constraints`` — the new
message's mentions merge in and the resolution + traversal re-run.

``ConstraintSet`` is state and ``WorkoutPlan.resolution_notes`` is prose;
they are kept apart so the round-tripped state stays small and the trace
describes exactly one response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from neo4j import AsyncDriver
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app.agents.model import build_model, build_model_settings
from app.kg.resolver import ResolvedConcept, resolve_concepts
from app.kg.safety import (
    ExcludedExercise,
    ExclusionKind,
    InjuryConstraint,
    PoolConstraints,
    PoolExercise,
    PoolResult,
    RuleFiring,
    safe_exercise_pool,
)

logger = logging.getLogger(__name__)

# Seconds of setup/transition budgeted per exercise when fitting the window.
TRANSITION_SECONDS = 30
# The plan must land within these bounds of the requested window.
TIME_FIT_MAX_RATIO = 1.1
TIME_FIT_MIN_RATIO = 0.5


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------


class ConstraintSet(BaseModel):
    """The merged, serializable constraint state for one conversation.

    Pure state: the ids and flags the traversal acts on, and nothing else.
    Returned on every plan as ``constraints_used``; send it back as
    ``prior_constraints`` with a follow-up message to adjust the plan.

    The prose explaining *how* this set was derived is deliberately not here
    — it belongs to a single response, not to the carried-forward state, and
    lives on ``WorkoutPlan.resolution_notes``.
    """

    equipment_ids: list[str] | None = None
    injuries: list[InjuryConstraint] = Field(default_factory=list)
    exclude_concept_ids: list[str] = Field(default_factory=list)
    downrank_concept_ids: list[str] = Field(default_factory=list)

    def to_pool_constraints(self) -> PoolConstraints:
        return PoolConstraints(
            equipment_ids=self.equipment_ids,
            injuries=self.injuries,
            exclude_concept_ids=sorted(set(self.exclude_concept_ids)),
            downrank_concept_ids=sorted(set(self.downrank_concept_ids)),
        )


class WorkoutRequest(BaseModel):
    """Input to ``POST /api/workout``."""

    member_id: str
    prompt: str
    time_window_minutes: int = Field(ge=5, le=180)
    prior_constraints: ConstraintSet | None = None


class PlannedExercise(BaseModel):
    """One line of the plan, with the graph's justification attached."""

    exercise_id: str
    name: str
    sets: int
    reps: int | None = None
    duration_seconds: int | None = None
    rest_seconds: int
    coach_note: str | None = None
    estimated_seconds: int
    why_chosen: str
    fired_rules: list[RuleFiring] = Field(default_factory=list)
    pool_notes: list[str] = Field(default_factory=list)


class PlanSection(BaseModel):
    title: str
    exercises: list[PlannedExercise]


class WorkoutPlan(BaseModel):
    """The typed response of ``POST /api/workout`` (doubles as the schema)."""

    title: str
    rationale: str
    warmup: PlanSection
    main: PlanSection
    cooldown: PlanSection
    time_window_minutes: int
    estimated_duration_minutes: float
    filtered_out_for_safety: list[ExcludedExercise]
    other_exclusions: list[ExcludedExercise]
    constraints_used: ConstraintSet
    resolution_notes: list[str]
    pool_size: int


# ---------------------------------------------------------------------------
# LLM call A — constraint extraction
# ---------------------------------------------------------------------------


class ConstraintMentions(BaseModel):
    """Constraint-bearing phrases extracted from one coach message."""

    exclusions: list[str] = Field(
        default_factory=list,
        description="Exercises/movements/equipment the coach wants excluded, "
        "as the coach's own words, e.g. 'deadlifts'",
    )
    equipment_only: list[str] | None = Field(
        default=None,
        description="When the coach restricts available equipment ('only "
        "dumbbells and a kettlebell'), the allowed items; null otherwise",
    )
    injury_mentions: list[str] = Field(
        default_factory=list,
        description="Body parts or conditions mentioned as injured/bothering "
        "the member, e.g. 'left knee'",
    )
    emphasis: list[str] = Field(
        default_factory=list,
        description="Muscle groups or qualities the coach wants emphasised",
    )


extraction_agent = Agent(
    name="constraint-extractor",
    output_type=ConstraintMentions,
    instructions=(
        "You extract training constraints from a fitness coach's message about "
        "a workout to generate. Extract only what the coach actually said - "
        "never invent constraints. General intent ('lower body strength "
        "workout') is emphasis, not an exclusion. Phrases like 'no barbell, "
        "only dumbbells and a kettlebell' set equipment_only to the allowed "
        "items. 'Her left knee is bothering her' is an injury mention."
    ),
)


# ---------------------------------------------------------------------------
# Member defaults from KG 2 (no LLM)
# ---------------------------------------------------------------------------


class MemberDefaults(BaseModel):
    """A member's standing constraints plus the trace of how they resolved."""

    constraints: ConstraintSet
    notes: list[str] = Field(default_factory=list)


async def member_defaults(driver: AsyncDriver, member_id: str) -> MemberDefaults:
    """Load and resolve the member's standing constraints from KG 2.

    Equipment joins directly; dislikes and injury notes go through the
    resolver (dislikes as down-ranks — they are preferences, not safety; the
    injury's free-text note resolves to a Condition so the rule layer can
    fire, per docs/data-overview.md quirk on `joints_loaded`).

    Returns:
        MemberDefaults carrying the constraint set and one note per
        resolution attempt, including the failures the coach must see.
    """
    records, _, _ = await driver.execute_query(
        "MATCH (m:Member {id: $id}) "
        "RETURN m.dislikes AS dislikes, "
        "[(m)-[:HAS_EQUIPMENT]->(q) | q.id] AS equipment_ids, "
        "[(m)-[:HAS_INJURY]->(i) | i{.id, .notes, .status, .severity}] AS injuries",
        id=member_id,
    )
    if not records:
        return MemberDefaults(
            constraints=ConstraintSet(),
            notes=[f"member {member_id!r} not found in KG 2"],
        )
    record = records[0]
    constraints = ConstraintSet(equipment_ids=sorted(record["equipment_ids"]))
    notes: list[str] = []

    for dislike in record["dislikes"] or []:
        for hit in await resolve_concepts(driver, dislike):
            if hit.resolved and hit.concept_id:
                constraints.downrank_concept_ids.append(hit.concept_id)
                notes.append(
                    f"dislike {dislike!r} resolved to {hit.concept_id} "
                    f"({hit.match_method}) — down-ranked, not excluded"
                )
            else:
                notes.append(f"dislike {dislike!r} unresolved: {hit.reason}")

    for injury in record["injuries"] or []:
        hits = await resolve_concepts(
            driver, injury["notes"], context="injury", concept_types=["condition"]
        )
        hit = hits[0]
        if hit.resolved and hit.concept_id:
            constraints.injuries.append(
                InjuryConstraint(
                    condition_id=hit.concept_id,
                    status=injury["status"],
                    severity=injury["severity"],
                )
            )
            notes.append(
                f"injury {injury['id']} note resolved to condition "
                f"{hit.concept_id} ({hit.match_method}, score {hit.score:.2f})"
            )
        else:
            notes.append(
                f"injury {injury['id']} note did not resolve to a condition "
                f"({hit.reason}); its safety rules CANNOT be applied — "
                "surface this to the coach"
            )
    return MemberDefaults(constraints=constraints, notes=notes)


# ---------------------------------------------------------------------------
# Mention resolution + merge (no LLM)
# ---------------------------------------------------------------------------


async def _merge_mentions(
    driver: AsyncDriver,
    constraints: ConstraintSet,
    mentions: ConstraintMentions,
    notes: list[str],
) -> None:
    for text in mentions.exclusions:
        for hit in await resolve_concepts(driver, text):
            if hit.resolved and hit.concept_id:
                constraints.exclude_concept_ids.append(hit.concept_id)
                notes.append(
                    f"exclusion {text!r} resolved to {hit.concept_id} "
                    f"({hit.match_method})"
                )
            else:
                notes.append(f"exclusion {text!r} unresolved: {hit.reason}")

    if mentions.equipment_only is not None:
        allowed: list[str] = []
        for text in mentions.equipment_only:
            hits = await resolve_concepts(
                driver, text, concept_types=["equipment"]
            )
            hit = hits[0]
            if hit.resolved and hit.concept_id:
                allowed.append(hit.concept_id)
                notes.append(f"equipment {text!r} resolved to {hit.concept_id}")
            else:
                notes.append(f"equipment {text!r} unresolved: {hit.reason}")
        constraints.equipment_ids = sorted(set(allowed))
        notes.append(
            "equipment restricted to "
            f"{constraints.equipment_ids} (bodyweight exercises always qualify)"
        )

    for text in mentions.injury_mentions:
        hits = await resolve_concepts(driver, text, context="injury")
        hit = hits[0]
        if not (hit.resolved and hit.concept_id):
            notes.append(f"injury mention {text!r} unresolved: {hit.reason}")
            continue
        if hit.concept_type == "condition":
            if all(i.condition_id != hit.concept_id for i in constraints.injuries):
                constraints.injuries.append(
                    InjuryConstraint(condition_id=hit.concept_id)
                )
            notes.append(f"injury mention {text!r} resolved to {hit.concept_id}")
        else:
            # A joint, not a condition: the member's recorded injuries on that
            # joint (already in the constraint set via member defaults) are
            # what the rules can act on.
            covered = bool(constraints.injuries)
            notes.append(
                f"injury mention {text!r} resolved to {hit.concept_id} "
                f"({hit.concept_type}); "
                + (
                    "member's recorded injury constraints already apply"
                    if covered
                    else "no recorded condition for this member — rules "
                    "cannot filter on a joint alone; surface to the coach"
                )
            )

    for text in mentions.emphasis:
        notes.append(f"emphasis noted: {text!r} (passed to the planner)")


# ---------------------------------------------------------------------------
# LLM call B — plan composition from the safe pool
# ---------------------------------------------------------------------------


class DraftExercise(BaseModel):
    exercise_id: str = Field(description="Must be an id from the pool list")
    sets: int = Field(ge=1, le=8)
    reps: int | None = Field(
        default=None, description="For rep-based exercises (is_reps true)"
    )
    duration_seconds: int | None = Field(
        default=None, description="For duration-based exercises (is_reps false)"
    )
    rest_seconds: int = Field(ge=0, le=300)
    coach_note: str | None = None


class PlanDraft(BaseModel):
    title: str
    rationale: str = Field(
        description="2-3 sentences on how the plan serves the goals and window"
    )
    warmup: list[DraftExercise]
    main: list[DraftExercise]
    cooldown: list[DraftExercise]


@dataclass
class PlannerDeps:
    pool_by_id: dict[str, PoolExercise]
    time_window_minutes: int


def exercise_seconds(
    pooled: PoolExercise, draft: DraftExercise
) -> int:
    """Time estimate for one plan line, from the catalog's rep duration."""
    if pooled.is_reps and draft.reps:
        work = draft.sets * draft.reps * pooled.estimated_rep_duration
    else:
        work = draft.sets * (draft.duration_seconds or 0)
    return int(work + draft.sets * draft.rest_seconds + TRANSITION_SECONDS)


def plan_minutes(deps: PlannerDeps, draft: PlanDraft) -> float:
    total = sum(
        exercise_seconds(deps.pool_by_id[d.exercise_id], d)
        for section in (draft.warmup, draft.main, draft.cooldown)
        for d in section
        if d.exercise_id in deps.pool_by_id
    )
    return total / 60


planner_agent = Agent(
    name="workout-planner",
    deps_type=PlannerDeps,
    output_type=PlanDraft,
    retries=3,
    instructions=(
        "You are a strength coach composing a workout plan. You are given a "
        "pre-filtered pool of exercises that are ALL safe and feasible for "
        "this member - safety filtering already happened in the knowledge "
        "graph, so choose freely from the pool but NEVER reference an "
        "exercise id that is not in it.\n"
        "Structure: warmup (mobility/dynamic/therapeutic work, ~10-20% of the "
        "time), main (the strength/conditioning work serving the coach's "
        "goal), cooldown (stretching/regen, ~10-15%).\n"
        "Prefer higher-scored exercises; negatively scored ones are "
        "down-ranked (member dislikes or clinical caution) - use them only "
        "if nothing better fits.\n"
        "For rep-based exercises set `reps` (8-15 typical) and leave "
        "duration_seconds null; for duration-based exercises set "
        "`duration_seconds` (20-60s typical) and leave reps null.\n"
        "Fit the time window: per-exercise time is "
        "sets*reps*rep_seconds (or sets*duration_seconds) plus "
        "sets*rest_seconds plus 30s transition. Aim for 85-100% of the window."
    ),
)


@planner_agent.output_validator
async def validate_plan(
    ctx: RunContext[PlannerDeps], draft: PlanDraft
) -> PlanDraft:
    """Hard guarantees: pool membership, rep/duration shape, time fit."""
    problems: list[str] = []
    for section_name, section in (
        ("warmup", draft.warmup),
        ("main", draft.main),
        ("cooldown", draft.cooldown),
    ):
        for entry in section:
            pooled = ctx.deps.pool_by_id.get(entry.exercise_id)
            if pooled is None:
                problems.append(
                    f"{section_name}: exercise id {entry.exercise_id!r} is "
                    "NOT in the safe pool - it was filtered out or does not "
                    "exist. Use only ids from the pool list."
                )
                continue
            if pooled.is_reps and not entry.reps:
                problems.append(
                    f"{section_name}: {pooled.name} is rep-based; set `reps`."
                )
            if not pooled.is_reps and not entry.duration_seconds:
                problems.append(
                    f"{section_name}: {pooled.name} is duration-based; set "
                    "`duration_seconds`."
                )
    if not draft.main:
        problems.append("the main section is empty")
    if not problems:
        minutes = plan_minutes(ctx.deps, draft)
        window = ctx.deps.time_window_minutes
        if minutes > window * TIME_FIT_MAX_RATIO:
            problems.append(
                f"plan is ~{minutes:.0f} min for a {window} min window - "
                "remove exercises or reduce sets/rest"
            )
        elif (
            minutes < window * TIME_FIT_MIN_RATIO
            # A very small pool may simply not fill the window; a short plan
            # is then the honest outcome, not a validation failure.
            and len(ctx.deps.pool_by_id) >= 8
        ):
            problems.append(
                f"plan is only ~{minutes:.0f} min for a {window} min window - "
                "add exercises or sets"
            )
    if problems:
        raise ModelRetry("\n".join(problems))
    return draft


def _pool_prompt(pool: list[PoolExercise], window: int, coach_prompt: str,
                 emphasis: list[str], goals: list[str]) -> str:
    lines = [
        f"Coach request: {coach_prompt}",
        f"Time window: {window} minutes.",
    ]
    if goals:
        lines.append("Member goals: " + "; ".join(goals))
    if emphasis:
        lines.append("Emphasis from the coach: " + ", ".join(emphasis))
    lines.append(
        "\nSafe exercise pool (id | name | score | rep-based | "
        "sec/rep | muscles | patterns):"
    )
    for e in pool:
        lines.append(
            f"{e.exercise_id} | {e.name} | {e.score:+.2f} | "
            f"{'reps' if e.is_reps else 'duration'} | "
            f"{e.estimated_rep_duration} | "
            f"{', '.join(e.muscle_groups)} | {', '.join(e.movement_patterns)}"
        )
    return "\n".join(lines)


async def _member_goals(driver: AsyncDriver, member_id: str) -> list[str]:
    records, _, _ = await driver.execute_query(
        "MATCH (:Member {id: $id})-[:HAS_GOAL]->(g) "
        "RETURN g.text AS text ORDER BY g.priority, g.id",
        id=member_id,
    )
    return [record["text"] for record in records]


# ---------------------------------------------------------------------------
# The shallow loop
# ---------------------------------------------------------------------------


async def generate_workout(
    driver: AsyncDriver, request: WorkoutRequest
) -> WorkoutPlan:
    """Resolve → fetch pool → generate; returns the full provenance-bearing plan."""
    model = build_model()

    notes: list[str] = []
    if request.prior_constraints is not None:
        constraints = request.prior_constraints.model_copy(deep=True)
        # An adjustment turn starts from state, not prose: the previous
        # turn's derivation notes belong to the previous response.
        notes.append("carried the previous turn's constraint set forward")
    else:
        defaults = await member_defaults(driver, request.member_id)
        constraints = defaults.constraints
        notes.extend(defaults.notes)

    extraction = await extraction_agent.run(
        request.prompt,
        model=model,
        model_settings=build_model_settings(effort="low", max_tokens=1024),
    )
    mentions = extraction.output
    await _merge_mentions(driver, constraints, mentions, notes)

    pool_result: PoolResult = await safe_exercise_pool(
        driver, constraints.to_pool_constraints()
    )
    notes.extend(pool_result.notes)
    if not pool_result.included:
        raise ValueError(
            "no exercises survive the current constraints; relax equipment "
            "or exclusions"
        )

    goals = await _member_goals(driver, request.member_id)
    deps = PlannerDeps(
        pool_by_id={e.exercise_id: e for e in pool_result.included},
        time_window_minutes=request.time_window_minutes,
    )
    draft_run = await planner_agent.run(
        _pool_prompt(
            pool_result.included,
            request.time_window_minutes,
            request.prompt,
            mentions.emphasis,
            goals,
        ),
        model=model,
        deps=deps,
        model_settings=build_model_settings(effort="low", max_tokens=4096),
    )
    draft = draft_run.output

    def build_section(title: str, entries: list[DraftExercise]) -> PlanSection:
        planned = []
        for entry in entries:
            pooled = deps.pool_by_id[entry.exercise_id]
            planned.append(
                PlannedExercise(
                    exercise_id=entry.exercise_id,
                    name=pooled.name,
                    sets=entry.sets,
                    reps=entry.reps,
                    duration_seconds=entry.duration_seconds,
                    rest_seconds=entry.rest_seconds,
                    coach_note=entry.coach_note,
                    estimated_seconds=exercise_seconds(pooled, entry),
                    why_chosen=pooled.inclusion_path.description,
                    fired_rules=pooled.fired_rules,
                    pool_notes=pooled.notes,
                )
            )
        return PlanSection(title=title, exercises=planned)

    return WorkoutPlan(
        title=draft.title,
        rationale=draft.rationale,
        warmup=build_section("Warmup", draft.warmup),
        main=build_section("Main", draft.main),
        cooldown=build_section("Cooldown", draft.cooldown),
        time_window_minutes=request.time_window_minutes,
        estimated_duration_minutes=round(plan_minutes(deps, draft), 1),
        filtered_out_for_safety=[
            e for e in pool_result.excluded if e.kind is ExclusionKind.SAFETY
        ],
        other_exclusions=[
            e for e in pool_result.excluded if e.kind is not ExclusionKind.SAFETY
        ],
        constraints_used=constraints,
        resolution_notes=notes,
        pool_size=len(pool_result.included),
    )
