"""Integration tests for the copilot's retrieval tool (`fetch_context_slice`).

Every section model here mirrors properties that `scripts/build_kg.py` writes.
Nothing else ties the two together, so a renamed or retyped property would
otherwise surface as a validation error at request time — the coach's copilot
breaking on a question, long after a green build. These tests read the real
built graph and fail loudly instead.

They also pin the two data facts the models encode: a skipped session has no
RPE, and the chat thread's sender vocabulary is closed.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from neo4j import AsyncDriver

from app.agents.copilot import ALL_SECTIONS, fetch_context_slice
from app.kg.member_facts import ChatSender

MEMBER_ID = "mbr_01HX9JORDAN"


async def test_every_section_validates_against_the_built_graph(
    graph_driver: AsyncDriver,
) -> None:
    # The drift detector: if build_kg.py renames a property the Cypher
    # projects, this fails here rather than inside a live copilot turn.
    slice_ = await fetch_context_slice(graph_driver, MEMBER_ID, list(ALL_SECTIONS))

    assert slice_.profile is not None
    assert slice_.goals
    assert slice_.adherence is not None and slice_.adherence.weeks
    assert slice_.biomarkers is not None
    assert slice_.weight_trend
    assert slice_.labs
    assert slice_.workout_history
    assert slice_.chat_history
    assert slice_.coach_brief is not None and slice_.coach_brief.tasks
    assert slice_.churn_risk is not None
    assert slice_.injuries
    assert slice_.equipment


async def test_dates_are_parsed_not_passed_through_as_strings(
    graph_driver: AsyncDriver,
) -> None:
    # The graph stores ISO strings. Leaving them as `str` let anything through,
    # including a malformed date; parsing them is what makes the sorts below
    # chronological rather than lexicographic.
    slice_ = await fetch_context_slice(
        graph_driver, MEMBER_ID, ["profile", "weight_trend", "chat_history"]
    )
    assert isinstance(slice_.profile.member_since, date)
    assert isinstance(slice_.weight_trend[0].date, date)

    timestamps = [message.ts for message in slice_.chat_history]
    assert all(isinstance(ts, datetime) for ts in timestamps)
    assert timestamps == sorted(timestamps)
    # Tz-aware: the member's timezone is what makes "yesterday" meaningful.
    assert all(ts.tzinfo is not None for ts in timestamps)


async def test_skipped_workout_reports_no_rpe(graph_driver: AsyncDriver) -> None:
    # A skipped session carries rpe=None with duration_min=0. Typing rpe as a
    # plain int would have made this row unrepresentable; reading the absence
    # as a 0 would tell the coach the member trained very easily when in fact
    # they did not train at all.
    slice_ = await fetch_context_slice(graph_driver, MEMBER_ID, ["workout_history"])
    skipped = [w for w in slice_.workout_history if not w.completed]
    assert skipped, "the reference member has at least one skipped session"
    assert all(workout.rpe is None for workout in skipped)
    assert all(workout.rpe is not None for workout in slice_.workout_history if workout.completed)


async def test_chat_senders_stay_within_the_vocabulary(
    graph_driver: AsyncDriver,
) -> None:
    # The copilot renders member vs coach messages differently on both ends. A
    # third sender value arriving from ingest should fail here, not silently
    # render as if the coach had said it.
    slice_ = await fetch_context_slice(graph_driver, MEMBER_ID, ["chat_history"])
    senders = {message.sender for message in slice_.chat_history}
    assert senders
    assert senders <= {ChatSender.COACH, ChatSender.MEMBER}


async def test_unknown_member_is_reported_not_empty(
    graph_driver: AsyncDriver,
) -> None:
    # The route turns this into a 404. Returning an empty slice instead would
    # show the coach a blank dashboard for a typo'd id.
    with pytest.raises(ValueError, match="not found"):
        await fetch_context_slice(graph_driver, "mbr_does_not_exist", ["profile"])
