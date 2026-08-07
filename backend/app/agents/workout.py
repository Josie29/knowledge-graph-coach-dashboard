"""Workout generator agent (surface A, issue #8).

The runtime is a **shallow, deterministic loop** — resolve, fetch pool,
generate — with the LLM entered at exactly two points and never in charge of
safety:

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

Step 4 is a bounded tool loop rather than a single call: the model can price a
draft with ``check_plan_duration`` before committing to it. Summing
``sets*reps*rep_seconds`` across a dozen lines is the kind of arithmetic
language models get wrong, and a wrong total used to surface as a plan that
filled half the coach's window. The tool runs the validator's own arithmetic,
so checking is cheaper than being rejected. Everything the loop can reach is
still pure computation over the pool — no graph access, no safety decisions.

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
import math
from collections import Counter
from dataclasses import dataclass

from neo4j import AsyncDriver
from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    IncompleteToolCall,
    ModelRetry,
    RunContext,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model

from app.agents.model import build_model, build_model_settings
from app.kg.resolver import ResolvedConcept, resolve_concepts
from app.kg.safety import (
    ExcludedExercise,
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
# The plan must land within these bounds of the requested window. One band:
# `time_fit_band` is the only reader, `time_fit_instruction` quotes what it
# returns and `validate_plan` enforces it, so changing a ratio moves the
# promise and the enforcement together or not at all.
TIME_FIT_MAX_RATIO = 1.1
TIME_FIT_MIN_RATIO = 0.8
# Narrowest band worth asking a planner to hit, in minutes.
MIN_BAND_WIDTH_MINUTES = 2
# The output-retry budget is shared by every way a draft can be rejected --
# Pydantic's own schema validation, pool membership, rep/duration shape,
# duplicate ids, an empty main section, and the time band -- and the time check
# cannot even run until the rest are clean. At 3 a run that spends two attempts
# on shape errors gets a single shot at the duration.
PLANNER_RETRIES = 5
# The most one pool entry can plausibly contribute, used only to decide whether
# the time floor is reachable from a given pool. These mirror the top of the
# volume the planner's own instruction suggests (reps "8-15 typical",
# duration "20-60s typical"), so the bound stays consistent with what the model
# is actually told to prescribe.
CAPACITY_SETS = 4
CAPACITY_REPS = 15
CAPACITY_DURATION_SECONDS = 60
CAPACITY_REST_SECONDS = 90


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
    # Bounded because the time floor makes an absurd single line the cheapest
    # way to pass validation: one 600-second plank would clear the band without
    # composing a workout.
    reps: int | None = Field(
        default=None,
        ge=1,
        le=30,
        description="For rep-based exercises (is_reps true)",
    )
    duration_seconds: int | None = Field(
        default=None,
        ge=5,
        le=300,
        description="For duration-based exercises (is_reps false)",
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


class TimeFitBand(BaseModel):
    """The duration a plan must land in for one request, in whole minutes."""

    window: int
    minimum: int
    maximum: int


def time_fit_band(window_minutes: int) -> TimeFitBand:
    """The one time-fit band, quoted to the planner and enforced by the validator.

    Rounded *inward* so the instruction can state the same integers the
    validator compares against: a prompt promising "at least 28" while the
    validator accepts 27.6 reintroduces, in the decimal place, exactly the
    prompt/enforcement drift this function exists to close. Rounding the
    ceiling up would be the dangerous direction — a prompt looser than the gate.

    Args:
        window_minutes: The time window the coach asked for.

    Returns:
        The window alongside the inclusive minimum and maximum plan duration.
    """
    minimum = math.ceil(window_minutes * TIME_FIT_MIN_RATIO)
    return TimeFitBand(
        window=window_minutes,
        minimum=minimum,
        # The ratios alone leave a band about 0.3x the window wide, which at the
        # schema's 5-minute floor is a single minute — unhittable when every
        # exercise costs 30 seconds of transition before any work. Widen rather
        # than let short windows retry their way into a hard failure.
        maximum=max(
            math.floor(window_minutes * TIME_FIT_MAX_RATIO),
            minimum + MIN_BAND_WIDTH_MINUTES,
        ),
    )


def exercise_seconds(
    pooled: PoolExercise, draft: DraftExercise
) -> int:
    """Time estimate for one plan line, from the catalog's rep duration."""
    if pooled.is_reps and draft.reps:
        work = draft.sets * draft.reps * pooled.rep_seconds
    else:
        work = draft.sets * (draft.duration_seconds or 0)
    return int(work + draft.sets * draft.rest_seconds + TRANSITION_SECONDS)


def sections_minutes(
    deps: PlannerDeps, *sections: list[DraftExercise]
) -> float:
    """Estimated minutes for any set of plan sections.

    Split out from ``plan_minutes`` so ``check_plan_duration`` can price a
    draft the model has not assembled into a ``PlanDraft`` yet, guaranteeing
    the tool and the validator run the same arithmetic.
    """
    total = sum(
        exercise_seconds(deps.pool_by_id[d.exercise_id], d)
        for section in sections
        for d in section
        if d.exercise_id in deps.pool_by_id
    )
    return total / 60


def plan_minutes(deps: PlannerDeps, draft: PlanDraft) -> float:
    return sections_minutes(deps, draft.warmup, draft.main, draft.cooldown)


planner_agent = Agent(
    name="workout-planner",
    deps_type=PlannerDeps,
    output_type=PlanDraft,
    retries=PLANNER_RETRIES,
    instructions=(
        "You are a strength coach composing a workout plan. You are given a "
        "pre-filtered pool of exercises that are ALL safe and feasible for "
        "this member - safety filtering already happened in the knowledge "
        "graph, so choose freely from the pool but NEVER reference an "
        "exercise id that is not in it. Use each exercise at most once.\n"
        "Structure: warmup (mobility/dynamic/therapeutic work, ~10-20% of the "
        "time), main (the strength/conditioning work serving the coach's "
        "goal), cooldown (stretching/regen, ~10-15%).\n"
        "Prefer higher-scored exercises; negatively scored ones are "
        "down-ranked (member dislikes or clinical caution) - use them only "
        "if nothing better fits.\n"
        "For rep-based exercises set `reps` (8-15 typical) and leave "
        "duration_seconds null; for duration-based exercises set "
        "`duration_seconds` (20-60s typical) and leave reps null."
    ),
)


@planner_agent.instructions
def time_fit_instruction(ctx: RunContext[PlannerDeps]) -> str:
    """The time budget, in the whole minutes ``validate_plan`` will enforce.

    Dynamic rather than baked into the static preamble for two reasons. The
    numbers come from ``time_fit_band``, so the promise made here and the gate
    applied later cannot drift. And a percentage of an unstated window is
    something the model has to compute before it can obey, where whole minutes
    it can simply hit — the arithmetic is what it is worst at.

    Registering it as a dynamic instruction is deliberate and cache-safe: the
    static preamble above ships as one non-dynamic part and this appends after
    it, and the Anthropic backend places the cache breakpoint after the last
    non-dynamic part, so the prefix cached by ``anthropic_cache_instructions``
    (see ``app/agents/model.py``) survives the per-request tail.
    """
    band = time_fit_band(ctx.deps.time_window_minutes)
    return (
        f"Time budget. The coach has {band.window} minutes. Target "
        f"{band.window} minutes and land just under it — a plan that runs over "
        "costs the coach time they do not have.\n"
        f"A plan totalling under {band.minimum} minutes or over "
        f"{band.maximum} minutes is rejected and you will be made to redo it. "
        "Do not work the total out yourself: call `check_plan_duration` with "
        "your draft and adjust until `within_band` is true, then return that "
        "plan.\n"
        f"If it comes up short, add exercises from the pool or add sets. Never "
        "pad `rest_seconds` to reach the floor — longer rest is not more "
        "training."
    )


class PlanDurationCheck(BaseModel):
    """What a draft plan costs in time, against the band it has to land in."""

    total_minutes: float
    window_minutes: int
    required_minimum_minutes: int
    required_maximum_minutes: int
    within_band: bool
    adjust_by_minutes: float = Field(
        description="Minutes to add (positive) or cut (negative); 0 when in band"
    )
    per_exercise_minutes: dict[str, float] = Field(
        description="Minutes per exercise id, to show where the time is going"
    )


@planner_agent.tool
async def check_plan_duration(
    ctx: RunContext[PlannerDeps],
    warmup: list[DraftExercise],
    main: list[DraftExercise],
    cooldown: list[DraftExercise],
) -> PlanDurationCheck:
    """Price a draft plan against the required time band. Call before finalising.

    Runs the same arithmetic the output validator runs, so a draft this reports
    as ``within_band`` cannot be rejected on time.

    Args:
        warmup: The draft's warmup lines.
        main: The draft's main lines.
        cooldown: The draft's cooldown lines.

    Returns:
        The total, the band, whether it fits, how far off it is, and a
        per-exercise breakdown showing where the time is going.
    """
    band = time_fit_band(ctx.deps.time_window_minutes)
    total = sections_minutes(ctx.deps, warmup, main, cooldown)
    if total < band.minimum:
        adjust = band.minimum - total
    elif total > band.maximum:
        adjust = band.maximum - total
    else:
        adjust = 0.0
    return PlanDurationCheck(
        total_minutes=round(total, 1),
        window_minutes=band.window,
        required_minimum_minutes=band.minimum,
        required_maximum_minutes=band.maximum,
        within_band=band.minimum <= total <= band.maximum,
        adjust_by_minutes=round(adjust, 1),
        per_exercise_minutes={
            d.exercise_id: round(
                exercise_seconds(ctx.deps.pool_by_id[d.exercise_id], d) / 60, 1
            )
            for section in (warmup, main, cooldown)
            for d in section
            if d.exercise_id in ctx.deps.pool_by_id
        },
    )


def _duplicate_ids(placed: list[DraftExercise]) -> list[str]:
    """Exercise ids appearing more than once across a draft's lines, sorted."""
    counts = Counter(d.exercise_id for d in placed)
    return sorted(exercise_id for exercise_id, n in counts.items() if n > 1)


def _pool_can_fill(deps: PlannerDeps, minimum_minutes: int) -> bool:
    """Whether this pool could plausibly reach the floor at all.

    The floor is only fair if the pool can actually reach it. Bounds the pool
    by using each exercise once at the top of the volume the instruction itself
    suggests, deliberately generously — this decides whether the floor is
    *reachable*, and a tight bound would turn "the pool is thin" into a hard
    failure. A three-exercise pool against a fifty-minute window is a real
    scenario (see ``docs/example-runs.md``), and there a short plan is the
    honest answer rather than a validation error.

    Args:
        deps: The planner's pool and window.
        minimum_minutes: The floor the plan would have to clear.

    Returns:
        True when the pool's upper bound reaches the floor, so the floor should
        be enforced; False when it cannot, so a short plan is accepted.
    """
    total = 0.0
    for pooled in deps.pool_by_id.values():
        work = (
            CAPACITY_SETS * CAPACITY_REPS * rep_seconds(pooled)
            if pooled.is_reps
            else CAPACITY_SETS * CAPACITY_DURATION_SECONDS
        )
        total += work + CAPACITY_SETS * CAPACITY_REST_SECONDS + TRANSITION_SECONDS
    return total / 60 >= minimum_minutes


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
    placed = [d for s in (draft.warmup, draft.main, draft.cooldown) for d in s]
    # Repeats would let a short plan reach the floor without adding variety, and
    # they belong in this pass rather than after the time gate so they cannot
    # spend a retry that the duration correction needs.
    repeated = _duplicate_ids(placed)
    if repeated:
        problems.append(
            f"these exercise ids appear more than once: {', '.join(repeated)}. "
            "Use each exercise at most once; add a different one from the pool "
            "instead."
        )
    if not problems:
        band = time_fit_band(ctx.deps.time_window_minutes)
        minutes = plan_minutes(ctx.deps, draft)
        if minutes > band.maximum:
            problems.append(
                f"the plan totals {minutes:.1f} min; it must not exceed "
                f"{band.maximum} min for this {band.window} min window, so it "
                f"is {minutes - band.maximum:.1f} min over. Drop the "
                "lowest-scored exercises from the main section, or remove a "
                "set from them; keep at least one exercise in the warmup and "
                "the cooldown."
            )
        elif minutes < band.minimum and _pool_can_fill(ctx.deps, band.minimum):
            per_line = minutes / len(placed) if placed else 0
            more = max(1, round((band.minimum - minutes) / per_line)) if per_line else 1
            problems.append(
                f"the plan totals {minutes:.1f} min; it must total at least "
                f"{band.minimum} min for this {band.window} min window, so it "
                f"is {band.minimum - minutes:.1f} min short. Its "
                f"{len(placed)} lines average {per_line:.1f} min each, so add "
                f"about {more} more exercise(s) from the pool to the main "
                f"section, or add a set to {more} of the exercises already "
                "there. Keep every exercise you have chosen and its "
                "reps/duration, and do NOT raise `rest_seconds` to close the "
                "gap. Call `check_plan_duration` on the revised draft before "
                "returning it."
            )
    if problems:
        raise ModelRetry("\n".join(problems))
    return draft


async def _run_planner(
    model: Model, prompt: str, deps: PlannerDeps, pool_size: int
) -> PlanDraft:
    """Compose the plan, turning an unusable draft into a coach-facing error.

    Args:
        model: The configured Claude model.
        prompt: The pool prompt to compose from.
        deps: The planner's pool and time window.
        pool_size: How many exercises the constraints left, for the error text.

    Returns:
        The validated draft.

    Raises:
        ValueError: When the planner cannot produce a plan inside the time
            band; the router renders this to the coach as a 422.
        IncompleteToolCall: When the output token budget was too small. That is
            a configuration fault rather than anything the coach can act on, so
            it deliberately escapes as a 500.
    """
    try:
        run = await planner_agent.run(
            prompt,
            model=model,
            deps=deps,
            model_settings=build_model_settings(effort="low", max_tokens=8192),
        )
    except IncompleteToolCall:
        raise
    except UnexpectedModelBehavior as exc:
        logger.error("Planner could not compose a plan in the time band: %s", exc)
        band = time_fit_band(deps.time_window_minutes)
        raise ValueError(
            f"could not compose a {band.minimum}-{band.maximum} minute plan "
            f"from the {pool_size} exercises this member's constraints allow. "
            "Try a different time window, or relax an exclusion."
        ) from exc
    return run.output


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
            f"{e.rep_seconds:.1f} | "
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
    draft = await _run_planner(
        model,
        _pool_prompt(
            pool_result.included,
            request.time_window_minutes,
            request.prompt,
            mentions.emphasis,
            goals,
        ),
        deps,
        len(pool_result.included),
    )

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
            e for e in pool_result.excluded if e.kind == "safety"
        ],
        other_exclusions=[
            e for e in pool_result.excluded if e.kind != "safety"
        ],
        constraints_used=constraints,
        resolution_notes=notes,
        pool_size=len(pool_result.included),
    )
