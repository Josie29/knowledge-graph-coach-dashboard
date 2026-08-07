from __future__ import annotations

import pytest
from pydantic_ai import IncompleteToolCall, UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.workout import (
    DraftExercise,
    PlanDraft,
    PlannerDeps,
    _run_planner,
    check_plan_duration,
    plan_minutes,
    planner_agent,
    time_fit_band,
    time_fit_instruction,
)
from app.kg.safety import GraphPath, PoolExercise

# The 15-exercise safe pool from docs/example-runs.md §1 ("Lower-body strength
# session, 50 minutes"), with `estimated_rep_duration` copied verbatim from
# data/exercises.json. Hard-coded rather than read from the dataset so that a
# change to the catalog shows up here as a failing test rather than silently
# re-baselining the assertion.
DEMO_POOL: list[tuple[str, float, bool]] = [
    ("Alternating Dumbbell Overhead Press", 0.4, True),
    ("Alternating Low Plank To Low Side Plank", 0.1, True),
    ("Bench-Lying Single-Arm Dumbbell Tricep Extension", 0.3, True),
    ("Bodyweight Pike", 0.3, True),
    ("Cow Pose", 0.0, False),
    ("Dumbbell Neutral-Grip Bench Press", 0.2, True),
    ("Ground Upper Trap Stretch", 0.0, False),
    ("High Plank Bird Dog", 0.2, True),
    ("Low Copenhagen Plank", 0.0, False),
    ("Push-Up to Knee-Drive", 1.2, True),
    ("Resistance Band Reverse Curl", 0.3, True),
    ("Standing Neck Circles", 0.1, True),
    ("Walking Toe Touches", 0.5, True),
    ("World's Greatest Stretch", 0.1, True),
    ("One-Kettlebell Hamstring Walkout", 0.3, True),
]


def _exercise_id(name: str) -> str:
    return "ex_" + name.lower().replace(" ", "_").replace("'", "")


def pooled(name: str, rep_duration: float, is_reps: bool) -> PoolExercise:
    """One pool entry with only the fields the time-fit math reads."""
    return PoolExercise(
        exercise_id=_exercise_id(name),
        name=name,
        supports_weight=False,
        estimated_rep_duration=rep_duration,
        is_reps=is_reps,
        muscle_groups=[],
        movement_patterns=[],
        equipment=[],
        inclusion_path=GraphPath(nodes=[], description="test fixture", cypher=""),
    )


def demo_deps(window_minutes: int = 50) -> PlannerDeps:
    return PlannerDeps(
        pool_by_id={
            e.exercise_id: e for e in (pooled(*row) for row in DEMO_POOL)
        },
        time_window_minutes=window_minutes,
    )


def line(
    name: str,
    sets: int,
    *,
    reps: int | None = None,
    duration_seconds: int | None = None,
    rest_seconds: int,
) -> DraftExercise:
    return DraftExercise(
        exercise_id=_exercise_id(name),
        sets=sets,
        reps=reps,
        duration_seconds=duration_seconds,
        rest_seconds=rest_seconds,
    )


def realistic_fifty_minute_session() -> PlanDraft:
    """A session a coach would sign off on, drawn from the demo pool.

    Eleven lines: three warmup, five main at 3-4 sets with 60-90s rest, three
    cooldown holds. Deliberately unremarkable — the point is that an ordinary
    good plan must land inside the band.
    """
    return PlanDraft(
        title="Upper-body strength, knee-safe",
        rationale="Fixture.",
        warmup=[
            line("World's Greatest Stretch", 2, reps=8, rest_seconds=45),
            line("Walking Toe Touches", 2, reps=10, rest_seconds=45),
            line("Standing Neck Circles", 2, reps=10, rest_seconds=30),
        ],
        main=[
            line("Dumbbell Neutral-Grip Bench Press", 4, reps=10, rest_seconds=90),
            line("Alternating Dumbbell Overhead Press", 3, reps=10, rest_seconds=90),
            line("Push-Up to Knee-Drive", 3, reps=10, rest_seconds=75),
            line("Resistance Band Reverse Curl", 3, reps=12, rest_seconds=60),
            line("High Plank Bird Dog", 3, reps=10, rest_seconds=60),
        ],
        cooldown=[
            line("Low Copenhagen Plank", 2, duration_seconds=40, rest_seconds=45),
            line("Cow Pose", 2, duration_seconds=40, rest_seconds=30),
            line("Ground Upper Trap Stretch", 2, duration_seconds=40, rest_seconds=30),
        ],
    )


class _FakeContext:
    """The only part of RunContext the instruction and the tool read."""

    def __init__(self, deps: PlannerDeps) -> None:
        self.deps = deps


def _common_prefix(left: str, right: str) -> str:
    end = 0
    while end < min(len(left), len(right)) and left[end] == right[end]:
        end += 1
    return left[:end]


class PlannerRun:
    """Drives `planner_agent` against canned drafts, with no model API.

    Returning the same draft on every call turns validation into a clean
    binary: an acceptable draft comes back on the first call, an unacceptable
    one exhausts the retry budget and raises. Either way the retry text the
    model would have seen is captured for inspection.
    """

    def __init__(self, *drafts: PlanDraft) -> None:
        self._drafts = drafts
        self.calls = 0
        self.retry_messages: list[str] = []

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        self.instructions = info.instructions or ""
        for message in messages:
            for part in getattr(message, "parts", []):
                if isinstance(part, RetryPromptPart):
                    text = part.model_response()
                    if text not in self.retry_messages:
                        self.retry_messages.append(text)
        draft = self._drafts[min(self.calls - 1, len(self._drafts) - 1)]
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, draft.model_dump())]
        )

    async def run(self, deps: PlannerDeps) -> PlanDraft:
        result = await planner_agent.run(
            "compose", model=FunctionModel(self._respond), deps=deps
        )
        return result.output


async def assert_accepted(draft: PlanDraft, deps: PlannerDeps) -> None:
    run = PlannerRun(draft)
    await run.run(deps)
    assert run.calls == 1, f"validator rejected: {run.retry_messages}"


async def assert_rejected(draft: PlanDraft, deps: PlannerDeps) -> str:
    run = PlannerRun(draft)
    with pytest.raises(UnexpectedModelBehavior):
        await run.run(deps)
    assert run.retry_messages
    return run.retry_messages[0]


# A synthetic pool whose lines cost exactly one minute each, so a draft can be
# built to land on an exact minute. Duration-based with 30s of work and no rest
# means one line costs 30s + 0s + TRANSITION_SECONDS = 60s.
MINUTE_POOL_SIZE = 240


def minute_deps(window_minutes: int) -> PlannerDeps:
    pool = [
        pooled(f"Minute Exercise {i}", 0.0, False) for i in range(MINUTE_POOL_SIZE)
    ]
    return PlannerDeps(
        pool_by_id={e.exercise_id: e for e in pool},
        time_window_minutes=window_minutes,
    )


def draft_of_minutes(minutes: int) -> PlanDraft:
    """A structurally valid draft from `minute_deps` costing exactly `minutes`."""
    lines = [
        line(
            f"Minute Exercise {i}", 1, duration_seconds=30, rest_seconds=0
        )
        for i in range(minutes)
    ]
    return PlanDraft(
        title="Fixture",
        rationale="Fixture.",
        warmup=lines[:1],
        main=lines[1:-1] or lines[:1],
        cooldown=lines[-1:] if len(lines) > 1 else [],
    )


def test_a_realistic_fifty_minute_session_is_estimated_realistically() -> None:
    # Catches the units bug in `exercise_seconds`: the catalog's
    # `estimated_rep_duration` is reps per second, not seconds per rep, so
    # multiplying by it made the work term ~13% of the estimate and left
    # rest_seconds as the only dial that moved the total. A coach's ordinary
    # 50-minute session was scored at ~40 minutes, and any floor tightened
    # against that ruler would push the model to inflate rest to reach it.
    minutes = plan_minutes(demo_deps(50), realistic_fifty_minute_session())

    assert 45 <= minutes <= 55, f"estimated {minutes:.1f} min for a 50 min session"


def test_the_work_term_is_a_meaningful_share_of_the_estimate() -> None:
    # Catches a re-inversion of the same bug that a total-minutes assertion
    # could still pass by coincidence. If reps and sets barely register, the
    # model can only reach a time target by padding rest — which is what the
    # duration estimate is supposed to prevent.
    deps = demo_deps(50)
    draft = realistic_fifty_minute_session()
    lines = [d for s in (draft.warmup, draft.main, draft.cooldown) for d in s]

    total_seconds = plan_minutes(deps, draft) * 60
    rest_seconds = sum(d.sets * d.rest_seconds for d in lines)
    transition_seconds = 30 * len(lines)
    work_seconds = total_seconds - rest_seconds - transition_seconds

    assert work_seconds / total_seconds > 0.25


def test_the_pool_prompt_quotes_the_same_seconds_the_estimator_uses() -> None:
    # Catches the half-fix: correcting `exercise_seconds` but leaving the pool
    # table showing the raw catalog rate. The column is labelled seconds, so
    # the model would price a 0.2 exercise at 0.2s per rep while the validator
    # priced it at 5s — a 25x disagreement that no amount of prompt wording
    # about hitting the window could survive.
    from app.agents.workout import _pool_prompt, rep_seconds

    bench = pooled("Dumbbell Neutral-Grip Bench Press", 0.2, True)
    prompt = _pool_prompt([bench], 50, "go", [], [])

    assert f"| {rep_seconds(bench):.1f} |" in prompt
    assert "| 0.2 |" not in prompt


@pytest.mark.parametrize("window", [5, 12, 35, 50, 180])
async def test_the_instruction_quotes_the_band_the_validator_enforces(window: int) -> None:
    # The reason this change exists. The prompt used to say "aim for 85-100%"
    # while the validator accepted 50-110%, so a plan at 56% of the window
    # passed silently. Anyone re-tuning a ratio without re-typing the prompt
    # would reopen exactly that gap; here the two are read from one helper and
    # this proves the quoted numbers are the enforced ones at both edges.
    band = time_fit_band(window)
    deps = minute_deps(window)

    text = time_fit_instruction(_FakeContext(deps))
    assert f"under {band.minimum} minutes" in text
    assert f"over {band.maximum} minutes" in text

    await assert_accepted(draft_of_minutes(band.minimum), deps)
    await assert_accepted(draft_of_minutes(band.maximum), deps)
    await assert_rejected(draft_of_minutes(band.minimum - 1), deps)
    await assert_rejected(draft_of_minutes(band.maximum + 1), deps)


async def test_the_varying_part_of_the_instruction_is_a_suffix() -> None:
    # Guards the prompt-cache property. The Anthropic backend puts the cache
    # breakpoint after the last static instruction part, so the preamble is
    # only cacheable while it stays byte-identical across requests. Folding the
    # window into the static string — or registering the preamble as a second
    # dynamic function — would silently cost a cache hit on every request.
    short = PlannerRun(draft_of_minutes(10))
    await short.run(minute_deps(12))
    long = PlannerRun(draft_of_minutes(45))
    await long.run(minute_deps(50))

    shared = _common_prefix(short.instructions, long.instructions)

    # Everything static survives into the shared prefix, all the way to its
    # last sentence — so the cacheable region covers the whole preamble.
    assert "You are a strength coach composing a workout plan." in shared
    assert "leave reps null." in shared

    # Everything that varies by window falls outside it, in each run's own tail.
    for run, window in ((short, 12), (long, 50)):
        band = time_fit_band(window)
        tail = run.instructions[len(shared):]
        assert f"under {band.minimum} minutes" in tail
        assert f"over {band.maximum} minutes" in tail


def test_the_transition_budget_is_interpolated_not_hardcoded(monkeypatch) -> None:
    # Catches a stale literal: the instruction used to re-type "30s transition"
    # next to TRANSITION_SECONDS = 30, so changing the constant would have left
    # the model budgeting against the old number.
    from app.agents import workout

    monkeypatch.setattr(workout, "TRANSITION_SECONDS", 45)
    deps = minute_deps(50)

    # The band is quoted in minutes, so the transition budget shows up in what
    # a line costs rather than in the instruction text.
    assert workout.exercise_seconds(
        pooled("x", 0.0, False),
        line("x", 1, duration_seconds=30, rest_seconds=0),
    ) == 75


async def test_a_plan_at_half_the_window_is_rejected_and_the_retry_says_how_short() -> None:
    # The reported symptom: 27.8 minutes returned for a 50-minute request. The
    # retry has to carry the arithmetic, because a model that could reliably do
    # this arithmetic would not have produced the short plan in the first place.
    deps = minute_deps(50)

    message = await assert_rejected(draft_of_minutes(28), deps)

    assert "28.0 min" in message
    assert "at least 40 min" in message
    assert "12.0 min short" in message
    assert "rest_seconds" in message  # must not close the gap by padding rest
    assert "check_plan_duration" in message


async def test_a_near_miss_retry_does_not_print_as_the_target_it_missed() -> None:
    # Catches the `.0f` formatting bug: a 39.6-minute plan against a 40-minute
    # floor rendered as "plan is only ~40 min ... at least 40 min", telling the
    # model its total was both wrong and correct.
    deps = minute_deps(50)
    draft = draft_of_minutes(40)
    draft.main[-1].rest_seconds = 0
    draft.main[-1].duration_seconds = 6  # 36s line: total 39.6 min

    message = await assert_rejected(draft, deps)

    assert "39.6 min" in message


async def test_a_plan_over_the_ceiling_says_how_much_to_cut() -> None:
    # The ceiling branch had no coverage at all, and it is the branch that
    # protects the coach's actual calendar.
    deps = minute_deps(50)

    message = await assert_rejected(draft_of_minutes(60), deps)

    assert "60.0 min" in message
    assert "not exceed 55 min" in message
    assert "5.0 min over" in message


async def test_the_tool_and_the_validator_agree() -> None:
    # The tool exists so the model can stop guessing at arithmetic, which only
    # helps if its verdict is the validator's verdict. If these ever diverge,
    # the model is being lied to by its own calculator and will burn the whole
    # retry budget acting on a number that was never going to be accepted.
    deps = minute_deps(50)
    band = time_fit_band(50)

    for minutes in (band.minimum - 1, band.minimum, 47, band.maximum, band.maximum + 1):
        draft = draft_of_minutes(minutes)
        check = await check_plan_duration(
            _FakeContext(deps), draft.warmup, draft.main, draft.cooldown
        )
        if check.within_band:
            await assert_accepted(draft, deps)
        else:
            await assert_rejected(draft, deps)


async def test_the_tool_reports_the_adjustment_needed() -> None:
    # A bare "too short" tells the model no more than the validator already
    # would. The signed delta is what lets it fix the draft in one pass instead
    # of guessing and re-checking.
    deps = minute_deps(50)
    draft = draft_of_minutes(28)

    check = await check_plan_duration(
        _FakeContext(deps), draft.warmup, draft.main, draft.cooldown
    )

    assert check.total_minutes == 28.0
    assert not check.within_band
    assert check.adjust_by_minutes == 12.0
    assert check.per_exercise_minutes


async def test_a_pool_too_small_to_fill_the_window_yields_a_short_plan_not_an_error() -> None:
    # Guards demo scenario 2 in docs/example-runs.md: the equipment-restricted
    # adjustment leaves 3 exercises against the same 50-minute window. Holding
    # that to a 40-minute floor would retry until it 422'd, turning the
    # narrowest safe pool — the most clinically interesting case — into an
    # error page.
    pool = [pooled(f"Only Exercise {i}", 0.0, False) for i in range(3)]
    deps = PlannerDeps(
        pool_by_id={e.exercise_id: e for e in pool}, time_window_minutes=50
    )
    lines = [
        line(f"Only Exercise {i}", 3, duration_seconds=60, rest_seconds=60)
        for i in range(3)
    ]
    draft = PlanDraft(
        title="Short but honest",
        rationale="Fixture.",
        warmup=lines[:1],
        main=lines[1:2],
        cooldown=lines[2:],
    )

    assert plan_minutes(deps, draft) < 40
    await assert_accepted(draft, deps)


async def test_a_pool_that_can_fill_the_window_is_still_held_to_the_floor() -> None:
    # The counterpart, and the pair a bare `len(pool) >= 8` could not tell
    # apart: the exemption must depend on the window, not just the pool count.
    # Without this, "the pool is small" becomes a way to skip the floor at any
    # window size.
    deps = minute_deps(50)

    await assert_rejected(draft_of_minutes(30), deps)


async def test_repeating_an_exercise_is_not_a_way_to_fill_the_window() -> None:
    # With a floor to clear, listing the same exercise three times is cheaper
    # than composing a real plan, and plan_minutes would happily count it.
    deps = minute_deps(50)
    draft = draft_of_minutes(45)
    draft.main[1].exercise_id = draft.main[0].exercise_id

    message = await assert_rejected(draft, deps)

    assert "more than once" in message


async def test_shape_errors_and_the_time_floor_share_one_retry_budget() -> None:
    # Documents the interaction that makes PLANNER_RETRIES load-bearing: the
    # time check is gated behind the shape checks, so attempts spent on a bad
    # exercise id are attempts the duration correction never gets. At the old
    # budget of 3 a run could reach the floor with a single try left.
    deps = minute_deps(50)
    bad_id = draft_of_minutes(45)
    bad_id.main[0].exercise_id = "ex_not_in_pool"
    too_short = draft_of_minutes(30)
    good = draft_of_minutes(45)

    run = PlannerRun(bad_id, too_short, too_short, good)
    await run.run(deps)

    assert run.calls == 4
    assert any("NOT in the safe pool" in m for m in run.retry_messages)
    assert any("min short" in m for m in run.retry_messages)


async def test_retry_exhaustion_becomes_a_coach_readable_error() -> None:
    # Without this translation the framework's "Exceeded maximum output
    # retries" escapes as a 500. The router already renders ValueError as a 422
    # whose detail goes straight into the UI alert, so the message has to name
    # the window and the pool size and tell the coach what to change.
    deps = minute_deps(50)
    run = PlannerRun(draft_of_minutes(20))

    with pytest.raises(ValueError) as caught:
        await _run_planner(
            FunctionModel(run._respond), "compose", deps, pool_size=7
        )

    detail = str(caught.value)
    assert "40-55 minute plan" in detail
    assert "7 exercises" in detail
    assert not isinstance(caught.value, UnexpectedModelBehavior)


async def test_a_token_limit_is_not_reported_as_a_client_error() -> None:
    # IncompleteToolCall subclasses UnexpectedModelBehavior, so a blanket catch
    # would relabel "max_tokens was too small" as a constraint problem the
    # coach could fix by changing the window. It is a config fault and has to
    # stay a 500 — this change makes it likelier, since the planner now emits
    # its draft once as tool arguments and again as output.
    def truncate(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, '{"title": "trunc')],
            finish_reason="length",
        )

    with pytest.raises(IncompleteToolCall):
        await _run_planner(
            FunctionModel(truncate), "compose", minute_deps(50), pool_size=7
        )
