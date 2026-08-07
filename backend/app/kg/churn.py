from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter

# Every threshold the classifier uses lives here so that
# docs/churn-risk-classification.md and the code quote the same numbers.
#
# The scale is deliberately small and hand-set: there is one member in this
# dataset, so these are reasoned thresholds, not fitted ones. The one property
# worth preserving if they are ever retuned is that no single signal can reach
# the ELEVATED band alone (max single signal = 3, band floor = 5) — an elevated
# member always has at least two independent things going wrong.

# How many weeks each side of the adherence trend comparison. A 2-vs-2 mean
# smooths the single-week noise that a week-over-week diff would amplify.
_TREND_WINDOW_WEEKS = 2

# (minimum drop in percentage points, points awarded), most severe first.
_DROP_THRESHOLDS: tuple[tuple[float, int], ...] = ((20.0, 3), (10.0, 2), (5.0, 1))

# (exclusive upper bound on the latest week's completion %, points awarded).
_FLOOR_THRESHOLDS: tuple[tuple[float, int], ...] = ((60.0, 2), (80.0, 1))

# (minimum skipped sessions in the window, points awarded).
_SKIP_THRESHOLDS: tuple[tuple[int, int], ...] = ((2, 2), (1, 1))
_SKIP_WINDOW_DAYS = 28

# (minimum days since the last completed workout, points awarded). Inclusive,
# so 15 days encodes "more than a fortnight", 8 encodes "more than a week".
_SILENCE_THRESHOLDS: tuple[tuple[int, int], ...] = ((15, 3), (11, 2), (8, 1))

# Band floors: score >= _BAND_ELEVATED is elevated, >= _BAND_MODERATE moderate.
_BAND_MODERATE = 2
_BAND_ELEVATED = 5

MAX_SCORE = 10


class ChurnRiskLevel(StrEnum):
    """The band a member's churn score falls into."""

    LOW = "low"
    MODERATE = "moderate"
    ELEVATED = "elevated"


class ChurnSignalName(StrEnum):
    """The fixed set of warning signs the classifier looks for."""

    ADHERENCE_DROP = "adherence_drop"
    ADHERENCE_FLOOR = "adherence_floor"
    SKIPPED_SESSIONS = "skipped_sessions"
    WORKOUT_SILENCE = "workout_silence"


class AdherenceWeek(BaseModel):
    """One week of completion percentage, as it ships in the member file."""

    week_of: date
    pct: float


class WorkoutRecord(BaseModel):
    """One workout-history entry. Only the scheduling fields matter here."""

    date: date
    planned: bool = True
    completed: bool = False


class ChurnSignal(BaseModel):
    """One warning sign that fired, with the number that triggered it."""

    name: ChurnSignalName
    points: int
    reason: str = Field(
        description="Coach-readable sentence citing the value that fired this "
        "signal. This is the audit trail the copilot quotes."
    )


class ChurnAssessment(BaseModel):
    """The computed churn classification for one member at one point in time."""

    level: ChurnRiskLevel
    score: int
    max_score: int = MAX_SCORE
    signals: list[ChurnSignal] = Field(
        description="Only the signals that fired, most points first."
    )

    @property
    def reasons(self) -> list[str]:
        """The firing signals' reason strings, in the same order as `signals`."""
        return [signal.reason for signal in self.signals]

    def to_graph_props(self, assessment_id: str) -> dict[str, Any]:
        """Flatten to Neo4j-storable properties.

        Neo4j property values cannot be nested maps, so the signal list is
        written as three parallel scalar arrays rather than a list of objects.

        Args:
            assessment_id: The node's `id`, unique within `:MemberFact`.

        Returns:
            A property map ready to hand to a Cypher `SET n += $props`.
        """
        return {
            "id": assessment_id,
            "level": str(self.level),  # StrEnum -> str for the Neo4j driver
            "score": self.score,
            "max_score": self.max_score,
            "signal_names": [str(signal.name) for signal in self.signals],
            "signal_points": [signal.points for signal in self.signals],
            "reasons": self.reasons,
        }


_ADHERENCE_ADAPTER = TypeAdapter(list[AdherenceWeek])
_WORKOUT_ADAPTER = TypeAdapter(list[WorkoutRecord])


def _format_pct(value: float) -> str:
    """Render a percentage without a trailing ``.0`` — 62.5 -> "62.5", 100.0 -> "100"."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _points_for(value: float, thresholds: Sequence[tuple[float, int]]) -> int:
    """Award the points of the first threshold `value` meets or exceeds."""
    for minimum, points in thresholds:
        if value >= minimum:
            return points
    return 0


def _score_adherence_drop(weeks: list[AdherenceWeek]) -> ChurnSignal | None:
    """Compare the last two weeks' mean completion against the two before it.

    Returns None when the signal does not fire, including when there is less
    than a full 2-vs-2 window of history (fewer than four weeks).
    """
    if len(weeks) < _TREND_WINDOW_WEEKS * 2:
        return None
    recent = weeks[-_TREND_WINDOW_WEEKS:]
    prior = weeks[-_TREND_WINDOW_WEEKS * 2 : -_TREND_WINDOW_WEEKS]
    recent_mean = sum(week.pct for week in recent) / len(recent)
    prior_mean = sum(week.pct for week in prior) / len(prior)
    drop = prior_mean - recent_mean
    points = _points_for(drop, _DROP_THRESHOLDS)
    if not points:
        return None
    return ChurnSignal(
        name=ChurnSignalName.ADHERENCE_DROP,
        points=points,
        reason=(
            f"Adherence fell {_format_pct(prior_mean)}% -> "
            f"{_format_pct(recent_mean)}% ({_TREND_WINDOW_WEEKS}-week average "
            f"vs the prior {_TREND_WINDOW_WEEKS} weeks), a "
            f"{_format_pct(drop)} point drop"
        ),
    )


def _score_adherence_floor(weeks: list[AdherenceWeek]) -> ChurnSignal | None:
    """Flag a low current completion rate regardless of which way it is moving."""
    latest = weeks[-1]
    points = 0
    bound = 0.0
    # Ascending bounds: take the first (strictest) one the value falls under.
    for upper_bound, candidate in _FLOOR_THRESHOLDS:
        if latest.pct < upper_bound:
            points, bound = candidate, upper_bound
            break
    if not points:
        return None
    return ChurnSignal(
        name=ChurnSignalName.ADHERENCE_FLOOR,
        points=points,
        reason=(
            f"Week of {latest.week_of.isoformat()} finished at "
            f"{_format_pct(latest.pct)}%, below the {_format_pct(bound)}% floor"
        ),
    )


def _score_skipped_sessions(
    workouts: list[WorkoutRecord], now_anchor: date
) -> ChurnSignal | None:
    """Count sessions that were planned but not completed in the recent window."""
    window_start = now_anchor - timedelta(days=_SKIP_WINDOW_DAYS)
    skipped = [
        workout
        for workout in workouts
        if workout.planned
        and not workout.completed
        and window_start <= workout.date <= now_anchor
    ]
    points = _points_for(len(skipped), _SKIP_THRESHOLDS)
    if not points:
        return None
    dates = ", ".join(sorted(workout.date.isoformat() for workout in skipped))
    plural = "s" if len(skipped) != 1 else ""
    return ChurnSignal(
        name=ChurnSignalName.SKIPPED_SESSIONS,
        points=points,
        reason=(
            f"{len(skipped)} planned session{plural} skipped in the "
            f"{_SKIP_WINDOW_DAYS} days to {now_anchor.isoformat()} ({dates})"
        ),
    )


def _score_workout_silence(
    workouts: list[WorkoutRecord], now_anchor: date
) -> ChurnSignal | None:
    """Measure how long it has been since the member last finished a workout."""
    completed = [
        workout
        for workout in workouts
        if workout.completed and workout.date <= now_anchor
    ]
    if not completed:
        return ChurnSignal(
            name=ChurnSignalName.WORKOUT_SILENCE,
            points=max(points for _, points in _SILENCE_THRESHOLDS),
            reason=(
                f"No completed workout on record as of {now_anchor.isoformat()}"
            ),
        )
    last = max(workout.date for workout in completed)
    days = (now_anchor - last).days
    points = _points_for(days, _SILENCE_THRESHOLDS)
    if not points:
        return None
    return ChurnSignal(
        name=ChurnSignalName.WORKOUT_SILENCE,
        points=points,
        reason=(
            f"{days} days since the last completed workout "
            f"({last.isoformat()}), as of {now_anchor.isoformat()}"
        ),
    )


def _band(score: int) -> ChurnRiskLevel:
    if score >= _BAND_ELEVATED:
        return ChurnRiskLevel.ELEVATED
    if score >= _BAND_MODERATE:
        return ChurnRiskLevel.MODERATE
    return ChurnRiskLevel.LOW


def assess_churn_risk(
    adherence_weeks: Sequence[Mapping[str, Any]],
    workouts: Sequence[Mapping[str, Any]],
    now_anchor: str | date,
) -> ChurnAssessment:
    """Classify a member's churn risk from their adherence and workout history.

    Four independent warning signs are scored and summed; the total selects a
    band. Every signal that fires records the value that triggered it, so a
    coach can always trace the level back to a number in the member's data.

    All recency is measured against `now_anchor` — the dataset's "today", taken
    from the coach brief's `generated_for` date — never the wall clock.

    Args:
        adherence_weeks: Weekly completion rows with `week_of` and `pct`. Order
            does not matter; they are sorted by date internally.
        workouts: Workout-history rows with `date`, `planned` and `completed`.
        now_anchor: The date to measure recency against, as an ISO-8601 string
            or a `date`.

    Returns:
        The assessment, with only the signals that fired, most points first.

    Raises:
        ValueError: If `adherence_weeks` is empty, or if any row or the anchor
            fails to parse (Pydantic's `ValidationError` is a `ValueError`).
    """
    if not adherence_weeks:
        raise ValueError("cannot assess churn risk without any adherence weeks")
    anchor = (
        date.fromisoformat(now_anchor)
        if isinstance(now_anchor, str)
        else now_anchor
    )
    weeks = sorted(
        _ADHERENCE_ADAPTER.validate_python(adherence_weeks),
        key=lambda week: week.week_of,
    )
    history = _WORKOUT_ADAPTER.validate_python(workouts)

    signals = [
        signal
        for signal in (
            _score_adherence_drop(weeks),
            _score_adherence_floor(weeks),
            _score_skipped_sessions(history, anchor),
            _score_workout_silence(history, anchor),
        )
        if signal is not None
    ]
    signals.sort(key=lambda signal: signal.points, reverse=True)
    score = sum(signal.points for signal in signals)
    return ChurnAssessment(level=_band(score), score=score, signals=signals)
