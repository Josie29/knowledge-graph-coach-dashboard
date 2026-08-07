"""Unit tests for the computed churn classifier (`app.kg.churn`).

The classifier is what the coach dashboard's churn badge and the copilot's
churn answers both rest on, so a silent retune here changes what a coach is
told about a member without anything else failing. These tests are pure — no
Neo4j, no LLM — and pin both the individual signal thresholds and the banding.

The reference expectations track the shipped member (Jordan Rivera) and the
worked example in docs/churn-risk-classification.md.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.kg.churn import (
    _BAND_ELEVATED,
    _DROP_THRESHOLDS,
    _FLOOR_THRESHOLDS,
    _SILENCE_THRESHOLDS,
    _SKIP_THRESHOLDS,
    ChurnRiskLevel,
    ChurnSignalName,
    assess_churn_risk,
)

ANCHOR = "2026-06-04"
_LAST_WEEK = "2026-06-02"

# A member doing everything right: flat 100% adherence, worked out yesterday.
# Signal tests start from this and perturb exactly one dimension, so a failure
# names the signal that broke rather than an entangled total.
_HEALTHY_WEEKS = (100.0, 100.0, 100.0, 100.0)
_RECENT_WORKOUT = [{"date": "2026-06-03", "planned": True, "completed": True}]


def _weeks(*pcts: float, end: str = _LAST_WEEK) -> list[dict[str, object]]:
    """Consecutive weekly adherence rows, oldest first, ending at `end`."""
    last = date.fromisoformat(end)
    return [
        {
            "week_of": (last - timedelta(weeks=len(pcts) - 1 - i)).isoformat(),
            "pct": pct,
        }
        for i, pct in enumerate(pcts)
    ]


def _workout(day: str, *, planned: bool = True, completed: bool = True) -> dict:
    return {"date": day, "planned": planned, "completed": completed}


def _points(assessment, name: ChurnSignalName) -> int:
    """Points awarded to one signal, or 0 when it did not fire."""
    return next((s.points for s in assessment.signals if s.name == name), 0)


# ---- The shipped member ----------------------------------------------------


def test_reference_member_scores_six_and_lands_elevated() -> None:
    # Guards the worked example the docs publish and the number the dashboard
    # badge shows for the demo member. If ingest, thresholds, or the anchor
    # change, the coach-visible answer to "is Jordan at risk?" changes with it.
    doc = json.loads(
        (Path(__file__).resolve().parents[2] / "data" / "member-context.json")
        .read_text()
    )
    assessment = assess_churn_risk(
        doc["adherence"]["weekly_completion_pct"],
        doc["workout_history"],
        doc["coach_brief"]["generated_for"],
    )

    assert assessment.level is ChurnRiskLevel.ELEVATED
    assert assessment.score == 6
    assert [s.name for s in assessment.signals] == [
        ChurnSignalName.ADHERENCE_DROP,
        ChurnSignalName.ADHERENCE_FLOOR,
        ChurnSignalName.SKIPPED_SESSIONS,
    ]


def test_no_reason_mentions_an_untracked_signal() -> None:
    # Guards the whole point of computing this: the dataset ships a churn
    # reason about login frequency with no backing field anywhere (quirk 11).
    # A computed reason must never reintroduce it, because the copilot is told
    # the reasons list is the complete set of churn facts.
    doc = json.loads(
        (Path(__file__).resolve().parents[2] / "data" / "member-context.json")
        .read_text()
    )
    assessment = assess_churn_risk(
        doc["adherence"]["weekly_completion_pct"],
        doc["workout_history"],
        doc["coach_brief"]["generated_for"],
    )

    joined = " ".join(assessment.reasons).lower()
    assert "login" not in joined
    assert "app" not in joined


def test_every_reason_cites_a_number() -> None:
    # Guards the audit trail. A reason with no figure in it gives the coach
    # nothing to check, and the copilot is instructed to quote these verbatim
    # as citations.
    assessment = assess_churn_risk(
        _weeks(100, 100, 75, 50),
        [_workout("2026-05-29", completed=False)],
        ANCHOR,
    )

    assert assessment.signals
    for signal in assessment.signals:
        assert any(character.isdigit() for character in signal.reason)


# ---- Signal: adherence drop ------------------------------------------------


@pytest.mark.parametrize(
    ("recent_pct", "expected"),
    [(80.0, 3), (85.0, 2), (95.0, 1), (97.0, 0)],
)
def test_adherence_drop_scores_by_size_of_the_fall(
    recent_pct: float, expected: int
) -> None:
    # Guards the trend thresholds. These decide whether a coach is warned at
    # all about a member who is still training but training less.
    assessment = assess_churn_risk(
        _weeks(100, 100, recent_pct, recent_pct), _RECENT_WORKOUT, ANCHOR
    )

    assert _points(assessment, ChurnSignalName.ADHERENCE_DROP) == expected


def test_adherence_drop_needs_a_full_two_versus_two_window() -> None:
    # Guards against a fabricated trend for a new member: with three weeks of
    # history there is no honest 2-vs-2 comparison, so the signal must stay
    # silent rather than compare uneven windows.
    assessment = assess_churn_risk(_weeks(100, 40, 40), _RECENT_WORKOUT, ANCHOR)

    assert _points(assessment, ChurnSignalName.ADHERENCE_DROP) == 0


def test_improving_adherence_does_not_fire_the_drop_signal() -> None:
    # Guards against a sign error turning a recovering member into a churn
    # risk — the most embarrassing possible false positive for a coach.
    assessment = assess_churn_risk(
        _weeks(50, 50, 100, 100), _RECENT_WORKOUT, ANCHOR
    )

    assert _points(assessment, ChurnSignalName.ADHERENCE_DROP) == 0


def test_unordered_adherence_rows_produce_the_same_trend() -> None:
    # Guards against depending on the file's row order. Neo4j returns pattern
    # comprehensions unordered, so a caller can hand these over shuffled.
    ordered = _weeks(100, 100, 75, 50)
    assessment = assess_churn_risk(
        list(reversed(ordered)), _RECENT_WORKOUT, ANCHOR
    )

    assert _points(assessment, ChurnSignalName.ADHERENCE_DROP) == 3


# ---- Signal: adherence floor -----------------------------------------------


@pytest.mark.parametrize(
    ("pct", "expected"), [(50.0, 2), (59.0, 2), (60.0, 1), (79.0, 1), (80.0, 0)]
)
def test_adherence_floor_scores_the_latest_week_only(
    pct: float, expected: int
) -> None:
    # Guards the absolute-level check that catches a member who has been
    # consistently low all along — flat history means the trend signal cannot
    # see them, so without this they would score zero.
    assessment = assess_churn_risk(
        _weeks(pct, pct, pct, pct), _RECENT_WORKOUT, ANCHOR
    )

    assert _points(assessment, ChurnSignalName.ADHERENCE_FLOOR) == expected


# ---- Signal: skipped sessions ----------------------------------------------


@pytest.mark.parametrize(("skips", "expected"), [(0, 0), (1, 1), (2, 2), (3, 2)])
def test_skipped_sessions_score_by_count_and_cap(
    skips: int, expected: int
) -> None:
    # Guards the signal that reads intent rather than outcome: these are
    # sessions the member scheduled and then did not do.
    workouts = [
        _workout(f"2026-05-{20 + i:02d}", completed=False) for i in range(skips)
    ]
    assessment = assess_churn_risk(
        _weeks(*_HEALTHY_WEEKS), [*_RECENT_WORKOUT, *workouts], ANCHOR
    )

    assert _points(assessment, ChurnSignalName.SKIPPED_SESSIONS) == expected


def test_skips_outside_the_window_are_ignored() -> None:
    # Guards recency: a session missed months ago must not keep a member
    # flagged forever after they have gone back to training.
    assessment = assess_churn_risk(
        _weeks(*_HEALTHY_WEEKS),
        [*_RECENT_WORKOUT, _workout("2026-01-15", completed=False)],
        ANCHOR,
    )

    assert _points(assessment, ChurnSignalName.SKIPPED_SESSIONS) == 0


def test_unplanned_missing_workouts_are_not_skips() -> None:
    # Guards the planned/completed distinction. A day with no session on the
    # calendar is a rest day, not a skip; counting it would flag every member.
    assessment = assess_churn_risk(
        _weeks(*_HEALTHY_WEEKS),
        [*_RECENT_WORKOUT, _workout("2026-06-01", planned=False, completed=False)],
        ANCHOR,
    )

    assert _points(assessment, ChurnSignalName.SKIPPED_SESSIONS) == 0


# ---- Signal: workout silence -----------------------------------------------


@pytest.mark.parametrize(
    ("days_ago", "expected"), [(1, 0), (7, 0), (8, 1), (11, 2), (15, 3)]
)
def test_workout_silence_scores_by_days_since_the_last_completed_session(
    days_ago: int, expected: int
) -> None:
    # Guards the signal that catches disengagement adherence percentages can
    # miss — a member who has simply stopped showing up.
    last = date.fromisoformat(ANCHOR) - timedelta(days=days_ago)
    assessment = assess_churn_risk(
        _weeks(*_HEALTHY_WEEKS), [_workout(last.isoformat())], ANCHOR
    )

    assert _points(assessment, ChurnSignalName.WORKOUT_SILENCE) == expected


def test_no_completed_workout_at_all_scores_maximum_silence() -> None:
    # Guards the empty case: a member who has never finished a session must
    # not fall through to zero points because there is no date to subtract.
    assessment = assess_churn_risk(_weeks(*_HEALTHY_WEEKS), [], ANCHOR)

    assert _points(assessment, ChurnSignalName.WORKOUT_SILENCE) == 3


def test_recency_uses_the_anchor_not_the_wall_clock() -> None:
    # Guards data quirk 13. The dataset lives in mid-2026; scoring against the
    # real current date would make every member look abandoned (or not) purely
    # as a function of when the test suite happens to run.
    assessment = assess_churn_risk(
        _weeks(*_HEALTHY_WEEKS), _RECENT_WORKOUT, ANCHOR
    )

    assert assessment.score == 0
    assert assessment.level is ChurnRiskLevel.LOW


# ---- Banding ---------------------------------------------------------------


def test_flawless_member_scores_zero_and_bands_low() -> None:
    # Guards the negative case: full adherence and a recent session must
    # produce no reasons at all, so the UI has nothing to alarm a coach with.
    assessment = assess_churn_risk(
        _weeks(*_HEALTHY_WEEKS), _RECENT_WORKOUT, ANCHOR
    )

    assert assessment.signals == []
    assert assessment.level is ChurnRiskLevel.LOW


@pytest.mark.parametrize(
    ("weeks", "workouts", "expected_score", "expected_level"),
    [
        ((100, 100, 95, 95), [], 1, ChurnRiskLevel.LOW),
        ((50, 50, 50, 50), [], 2, ChurnRiskLevel.MODERATE),
        ((100, 100, 80, 80), [("2026-05-29",)], 4, ChurnRiskLevel.MODERATE),
        (
            (100, 100, 80, 80),
            [("2026-05-29",), ("2026-05-30",)],
            5,
            ChurnRiskLevel.ELEVATED,
        ),
    ],
)
def test_band_edges(
    weeks: tuple[float, ...],
    workouts: list[tuple[str]],
    expected_score: int,
    expected_level: ChurnRiskLevel,
) -> None:
    # Guards the 2 and 5 cut-points. These are the only thing standing between
    # a score and the word a coach actually reads on the dashboard.
    history = [_workout("2026-06-03")] + [
        _workout(day, completed=False) for (day,) in workouts
    ]
    assessment = assess_churn_risk(_weeks(*weeks), history, ANCHOR)

    assert assessment.score == expected_score
    assert assessment.level is expected_level


def test_no_single_signal_can_reach_the_elevated_band() -> None:
    # Guards the design property the thresholds were chosen for: "elevated"
    # must always mean at least two independent things went wrong. Retuning
    # any one signal's top score past the band floor breaks that promise
    # silently, so it is asserted against the constants themselves.
    tables = (
        _DROP_THRESHOLDS,
        _FLOOR_THRESHOLDS,
        _SKIP_THRESHOLDS,
        _SILENCE_THRESHOLDS,
    )
    worst_single_signal = max(
        points for table in tables for _, points in table
    )

    assert worst_single_signal < _BAND_ELEVATED


# ---- Input validation ------------------------------------------------------


def test_empty_adherence_history_is_rejected() -> None:
    # Guards against scoring a member we have no data for. Returning "low" for
    # an unknown member would be a confident, wrong reassurance.
    with pytest.raises(ValueError, match="adherence weeks"):
        assess_churn_risk([], _RECENT_WORKOUT, ANCHOR)


@pytest.mark.parametrize(
    ("weeks", "anchor"),
    [
        (_weeks(100, 100), "not-a-date"),
        ([{"week_of": "2026-06-02"}], ANCHOR),
        ([{"week_of": "the second of june", "pct": 50}], ANCHOR),
    ],
)
def test_malformed_input_raises_rather_than_scoring(weeks, anchor: str) -> None:
    # Guards the build: a bad row must stop `build_kg.py` loudly instead of
    # writing a plausible-looking but meaningless level into the graph.
    with pytest.raises(ValueError):
        assess_churn_risk(weeks, _RECENT_WORKOUT, anchor)


# ---- Graph serialisation ---------------------------------------------------


def test_graph_props_flatten_signals_into_parallel_arrays() -> None:
    # Guards the Neo4j write. Property values cannot be nested maps, so the
    # signal objects are split across three arrays that must stay index-aligned
    # — a mismatch would attribute the wrong points to the wrong reason.
    assessment = assess_churn_risk(
        _weeks(100, 100, 75, 50),
        [_workout("2026-05-29", completed=False)],
        ANCHOR,
    )
    props = assessment.to_graph_props("churn_2026-06-04")

    assert props["id"] == "churn_2026-06-04"
    assert props["level"] == "elevated"
    assert isinstance(props["level"], str)  # StrEnum must not reach the driver
    assert (
        len(props["signal_names"])
        == len(props["signal_points"])
        == len(props["reasons"])
        == len(assessment.signals)
    )
    assert sum(props["signal_points"]) == props["score"]
