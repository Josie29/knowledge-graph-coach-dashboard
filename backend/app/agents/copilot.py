"""Member-context copilot agent (surface B, issue #10).

The copilot answers coach questions about a member, grounded in KG 2. Its
single retrieval tool, ``member_context``, returns a typed ``ContextSlice``
of the requested graph sections (adherence, sleep, biomarkers, labs,
history, chat, brief, churn, injuries, equipment) anchored to the member's
``now_anchor`` date — never the wall clock (data quirk 13).

The typed output ``CopilotAnswer`` is a union: a ``TextAnswer`` or a
``ChartAnswer`` carrying a Recharts-shaped ``ChartSpec``, so the agent
requests charts declaratively and the browser renders them.

Grounding rules (enforced by instruction, verified by the citations field):

- every number cited must come from a ContextSlice the agent fetched;
- the "login frequency" churn reason has no backing field anywhere in the
  data (quirk 11) — it may only be attributed to the coach brief itself;
- no lab reference ranges ship with the data (quirk 12) — high/low/normal
  verdicts must either cite an explicitly-external range or be hedged.

Streaming: the FastAPI route dispatches requests through Pydantic AI's AG-UI
adapter; the client holds the conversation and sends the full message
history each run, which is what makes follow-up questions work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from neo4j import AsyncDriver
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

# ---------------------------------------------------------------------------
# ContextSlice — the retrieval tool's typed result
# ---------------------------------------------------------------------------

SectionName = Literal[
    "profile",
    "goals",
    "adherence",
    "biomarkers",
    "weight_trend",
    "labs",
    "workout_history",
    "chat_history",
    "coach_brief",
    "injuries",
    "equipment",
]

ALL_SECTIONS: tuple[SectionName, ...] = (
    "profile",
    "goals",
    "adherence",
    "biomarkers",
    "weight_trend",
    "labs",
    "workout_history",
    "chat_history",
    "coach_brief",
    "injuries",
    "equipment",
)


class AdherenceSlice(BaseModel):
    trend: str
    weeks: list[dict[str, str | int]]


class BiomarkerSlice(BaseModel):
    resting_hr_bpm: int
    hrv_ms: int
    sleep_hours_last_7_days: list[float]


class LabPanelSlice(BaseModel):
    panel_type: str
    date: str
    results: dict[str, float]


class CoachBriefSlice(BaseModel):
    generated_for: str
    churn_risk_level: str
    churn_risk_reasons: list[str]
    tasks: list[dict[str, str]]


class ContextSlice(BaseModel):
    """The member-context data returned to the agent, one field per section."""

    member_id: str
    now_anchor: str = Field(
        description="The dataset's 'today'. All recency/trend statements must "
        "be computed against this date, never the wall clock."
    )
    profile: dict[str, str | int | float] | None = None
    goals: list[dict[str, str | int | None]] | None = None
    adherence: AdherenceSlice | None = None
    biomarkers: BiomarkerSlice | None = None
    weight_trend: list[dict[str, str | float]] | None = None
    labs: list[LabPanelSlice] | None = None
    workout_history: list[dict[str, str | int | bool | list[str] | None]] | None = None
    chat_history: list[dict[str, str | bool | list[str] | None]] | None = None
    coach_brief: CoachBriefSlice | None = None
    injuries: list[dict[str, str | None]] | None = None
    equipment: list[str] | None = None


# ---------------------------------------------------------------------------
# CopilotAnswer — the typed output with a chart-spec union member
# ---------------------------------------------------------------------------


class ChartSeries(BaseModel):
    data_key: str = Field(description="Key in each data row for this series")
    label: str


class ChartSpec(BaseModel):
    """A declarative, Recharts-shaped chart request."""

    chart_type: Literal["line", "bar", "area"]
    title: str
    x_key: str = Field(description="Key in each data row for the x axis")
    series: list[ChartSeries]
    data: list[dict[str, str | float | int | None]] = Field(
        description="Rows keyed by x_key and every series data_key; values "
        "must come verbatim from a fetched ContextSlice"
    )
    y_label: str | None = None
    source: str = Field(
        description="Which KG 2 section(s) the data came from, e.g. "
        "'adherence weeks (KG 2)'"
    )


class TextAnswer(BaseModel):
    kind: Literal["text"]
    markdown: str
    citations: list[str] = Field(
        description="The KG 2 sections/values each claim rests on"
    )


class ChartAnswer(BaseModel):
    kind: Literal["chart"]
    markdown: str = Field(description="Short commentary to show beside the chart")
    chart: ChartSpec
    citations: list[str]


CopilotAnswer = TextAnswer | ChartAnswer


# ---------------------------------------------------------------------------
# Retrieval over KG 2
# ---------------------------------------------------------------------------


@dataclass
class CopilotDeps:
    driver: AsyncDriver
    member_id: str


async def fetch_context_slice(
    driver: AsyncDriver, member_id: str, sections: list[SectionName]
) -> ContextSlice:
    """Read the requested sections of the Member Context graph."""
    wanted = set(sections) or set(ALL_SECTIONS)
    records, _, _ = await driver.execute_query(
        "MATCH (m:Member {id: $id}) "
        "RETURN m{.*} AS member, "
        "[(m)-[:HAS_GOAL]->(g) | g{.id, .text, .priority, .target_date}] AS goals, "
        "[(m)-[:HAS_ADHERENCE_WEEK]->(w) | w{.week_of, .pct}] AS adherence_weeks, "
        "[(m)-[:HAS_BIOMARKERS]->(b) | b{.*}] AS biomarkers, "
        "[(m)-[:HAS_WEIGHT_SAMPLE]->(s) | s{.date, .kg}] AS weight_samples, "
        "[(m)-[:HAS_LAB_PANEL]->(p) | p{.id, .panel_type, .date, results: "
        "[(p)-[:HAS_RESULT]->(r) | r{.name, .value}]}] AS lab_panels, "
        "[(m)-[:PERFORMED]->(wo) | wo{.date, .title, .completed, .duration_min, "
        ".rpe, .exercise_names}] AS workouts, "
        "[(m)-[:HAS_CHAT_MESSAGE]->(c) | c{.ts, .sender, .text, .has_attachments, "
        ".attachment_types}] AS chat, "
        "[(m)-[:HAS_BRIEF]->(br) | br{.generated_for, .churn_risk_level, "
        ".churn_risk_reasons, tasks: [(br)-[:HAS_TASK]->(t) | "
        "t{.type, .text, .position}]}] AS briefs, "
        "[(m)-[:HAS_INJURY]->(i) | i{.region, .joint, .status, .severity, "
        ".since, .notes}] AS injuries, "
        "[(m)-[:HAS_EQUIPMENT]->(q) | q.catalog_term] AS equipment",
        id=member_id,
    )
    if not records:
        raise ValueError(f"member {member_id!r} not found in the graph")
    record = records[0]
    member = record["member"]

    slice_ = ContextSlice(
        member_id=member_id, now_anchor=str(member.get("now_anchor", ""))
    )
    if "profile" in wanted:
        slice_.profile = {
            key: member[key]
            for key in (
                "name", "age", "sex", "height_cm", "weight_kg", "tier",
                "member_since", "timezone", "preferred_session_minutes",
                "training_days_per_week", "adherence_trend",
            )
            if key in member
        }
    if "goals" in wanted:
        slice_.goals = sorted(
            record["goals"], key=lambda g: (g["priority"], g["id"])
        )
    if "adherence" in wanted:
        slice_.adherence = AdherenceSlice(
            trend=member["adherence_trend"],
            weeks=sorted(record["adherence_weeks"], key=lambda w: w["week_of"]),
        )
    if "biomarkers" in wanted and record["biomarkers"]:
        biomarkers = record["biomarkers"][0]
        slice_.biomarkers = BiomarkerSlice(
            resting_hr_bpm=biomarkers["resting_hr_bpm"],
            hrv_ms=biomarkers["hrv_ms"],
            sleep_hours_last_7_days=list(biomarkers["sleep_hours_last_7_days"]),
        )
    if "weight_trend" in wanted:
        slice_.weight_trend = sorted(
            record["weight_samples"], key=lambda s: s["date"]
        )
    if "labs" in wanted:
        slice_.labs = [
            LabPanelSlice(
                panel_type=panel["panel_type"],
                date=panel["date"],
                results={r["name"]: r["value"] for r in panel["results"]},
            )
            for panel in record["lab_panels"]
        ]
    if "workout_history" in wanted:
        slice_.workout_history = sorted(
            record["workouts"], key=lambda w: w["date"], reverse=True
        )
    if "chat_history" in wanted:
        slice_.chat_history = sorted(record["chat"], key=lambda c: c["ts"])
    if "coach_brief" in wanted and record["briefs"]:
        brief = record["briefs"][0]
        slice_.coach_brief = CoachBriefSlice(
            generated_for=str(brief["generated_for"]),
            churn_risk_level=brief["churn_risk_level"],
            churn_risk_reasons=list(brief["churn_risk_reasons"]),
            tasks=[
                {"type": t["type"], "text": t["text"]}
                for t in sorted(brief["tasks"], key=lambda t: t["position"])
            ],
        )
    if "injuries" in wanted:
        slice_.injuries = record["injuries"]
    if "equipment" in wanted:
        slice_.equipment = sorted(record["equipment"])
    return slice_


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

copilot_agent = Agent(
    name="member-copilot",
    deps_type=CopilotDeps,
    output_type=CopilotAnswer,  # type: ignore[arg-type]
    retries=2,
    instructions=(
        "You are a coach's copilot for one fitness club member. Answer the "
        "coach's questions using ONLY data fetched with the member_context "
        "tool - never invent numbers, dates, or facts. List every value you "
        "relied on in `citations` (section + values).\n"
        "\n"
        "Time: the dataset's 'today' is the ContextSlice's now_anchor. "
        "Compute 'this week' / 'since last week' / recency against it, "
        "never the real current date.\n"
        "\n"
        "Churn: the coach brief's churn reason about login frequency has no "
        "backing data anywhere in the graph. If you mention it, attribute it "
        "explicitly to the coach brief ('the brief cites...'), never as an "
        "observed fact.\n"
        "\n"
        "Labs: the member's lab values ship WITHOUT reference ranges. Never "
        "flatly declare a value high/low/normal. Either hedge ('7 of the "
        "panel values need clinical context to interpret') or, if you use a "
        "commonly cited range from general knowledge, say so explicitly "
        "('compared with commonly cited adult ranges - not from the "
        "member's chart') and recommend clinician confirmation.\n"
        "\n"
        "Charts: when the coach asks to plot, chart, visualise, or compare "
        "over time, return a ChartAnswer whose data rows come verbatim from "
        "the fetched slices (adherence trend -> line/bar by week_of; sleep "
        "-> bar by day; message pattern -> bar of messages per day from "
        "chat timestamps; 4-week comparison -> bar by week). Otherwise "
        "return a TextAnswer. Keep markdown concise and coach-friendly."
    ),
)


@copilot_agent.tool
async def member_context(
    ctx: RunContext[CopilotDeps], sections: list[SectionName]
) -> ContextSlice:
    """Fetch sections of this member's context graph (KG 2).

    Args:
        sections: Which sections the question needs — fetch only those.
            Available: profile, goals, adherence, biomarkers (incl. sleep),
            weight_trend, labs, workout_history, chat_history, coach_brief
            (incl. churn risk), injuries, equipment.
    """
    return await fetch_context_slice(
        ctx.deps.driver, ctx.deps.member_id, sections
    )
