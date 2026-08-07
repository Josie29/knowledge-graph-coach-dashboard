from __future__ import annotations

from app.agents.workout import (
    DraftExercise,
    PlanDraft,
    PlannerDeps,
    plan_minutes,
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
