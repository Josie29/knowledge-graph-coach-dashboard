from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.kg.rules import InjurySeverity, InjuryStatus


class ChatSender(StrEnum):
    """Who wrote a message in the coach/member thread."""

    COACH = "coach"
    MEMBER = "member"


class BriefTaskType(StrEnum):
    """The kind of action a morning-brief task asks the coach to take."""

    CELEBRATE = "celebrate"
    REVIEW_RISK = "review_risk"


class Goal(BaseModel):
    """A member's coaching goal."""

    id: str
    text: str
    priority: int
    target_date: date | None = None


class ProfileFact(BaseModel):
    """The member's profile and standing preferences, as KG 2 holds them.

    A narrower projection than ``app.members.MemberProfile``: that one backs
    the dashboard header (and carries ``id`` / ``coach_id``), this one is what
    the copilot reasons over.
    """

    name: str
    age: int
    sex: str
    height_cm: int
    weight_kg: float
    tier: str
    member_since: date
    timezone: str
    preferred_session_minutes: int
    training_days_per_week: int
    adherence_trend: str


class WeightSample(BaseModel):
    """One dated body-weight reading."""

    date: date
    kg: float


class WorkoutSummary(BaseModel):
    """One entry from the member's workout history.

    ``exercise_names`` is free text that matches nothing in the exercise
    catalog (docs/data-overview.md quirk 10) — it must not be joined against
    ``Exercise`` ids.
    """

    date: date
    title: str
    completed: bool
    duration_min: int
    rpe: int | None = Field(
        default=None,
        description="Rate of perceived exertion, absent for a session the "
        "member skipped. Absent is not the same as an easy session — a "
        "skipped workout also reports duration_min 0 and no exercises.",
    )
    exercise_names: list[str] = Field(default_factory=list)


class ChatMessage(BaseModel):
    """One message in the coach/member thread.

    ``ts`` is timezone-aware; recency against it must be computed from the
    member's ``now_anchor``, not the wall clock (quirk 13).
    """

    ts: datetime
    sender: ChatSender
    text: str
    has_attachments: bool
    attachment_types: list[str] = Field(default_factory=list)
    attachment_captions: list[str] = Field(default_factory=list)


class InjuryFact(BaseModel):
    """One recorded injury, in the member's own record rather than resolved.

    ``status`` / ``severity`` share the vocabulary the safety rules match on,
    so what a coach reads here and what ``safe_exercise_pool`` filters by
    cannot drift apart.
    """

    region: str
    joint: str
    status: InjuryStatus
    severity: InjurySeverity
    since: date
    notes: str


class BriefTask(BaseModel):
    """One action from the coach's morning brief."""

    type: BriefTaskType
    text: str
