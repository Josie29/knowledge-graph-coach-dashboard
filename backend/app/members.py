import asyncio
import json
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from neo4j import AsyncDriver
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/members", tags=["members"])

GRAPH_READ_TIMEOUT_SECONDS = 3.0


class Goal(BaseModel):
    """A member's coaching goal."""

    id: str
    text: str
    priority: int
    target_date: date | None


class MemberProfile(BaseModel):
    """Profile-header fields for the member view."""

    id: str
    name: str
    age: int
    sex: str
    tier: str
    member_since: date
    coach_id: str
    timezone: str


class MemberResponse(BaseModel):
    """Member context served to the dashboard: profile header plus goals."""

    profile: MemberProfile
    goals: list[Goal]


def _load_member_context(data_dir: Path) -> dict[str, object]:
    """Read and parse the member-context seed file.

    Args:
        data_dir: Directory containing ``member-context.json``.

    Returns:
        The parsed JSON document as a dict.

    Raises:
        HTTPException: 500 if the file is missing or not valid JSON.
    """
    context_path = data_dir / "member-context.json"
    try:
        raw = context_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except FileNotFoundError:
        logger.error("Member context file not found at %s", context_path)
        raise HTTPException(status_code=500, detail="Member data unavailable")
    except json.JSONDecodeError as exc:
        logger.error("Member context file at %s is not valid JSON: %s", context_path, exc)
        raise HTTPException(status_code=500, detail="Member data unavailable")
    if not isinstance(parsed, dict):
        logger.error("Member context file at %s is not a JSON object", context_path)
        raise HTTPException(status_code=500, detail="Member data unavailable")
    return parsed


def _member_from_seed(member_id: str) -> MemberResponse:
    """Serve the member from the seed file — the pre-ingest fallback path."""
    context = _load_member_context(settings.data_dir)
    profile_data = context.get("profile")
    if not isinstance(profile_data, dict) or profile_data.get("id") != member_id:
        raise HTTPException(status_code=404, detail=f"Member {member_id!r} not found")

    try:
        return MemberResponse(
            profile=MemberProfile.model_validate(profile_data),
            goals=[Goal.model_validate(goal) for goal in context.get("goals", [])],
        )
    except ValidationError as exc:
        logger.error("Member context for %s failed validation: %s", member_id, exc)
        raise HTTPException(status_code=500, detail="Member data unavailable")


async def _member_from_graph(
    driver: AsyncDriver, member_id: str
) -> MemberResponse | None:
    """Read the member from KG 2.

    Returns:
        The member, or None when the graph holds no ``Member`` nodes at all —
        meaning the KG build has not run yet, so the seed file is the honest
        source rather than a 404.

    Raises:
        HTTPException: 404 when the graph is populated but lacks this id.
    """
    records, _, _ = await driver.execute_query(
        "WITH count{(:Member)} AS total "
        "OPTIONAL MATCH (m:Member {id: $id}) "
        "RETURN total, m{.*} AS profile, "
        "[(m)-[:HAS_GOAL]->(g:Goal) | "
        "g{.id, .text, .priority, .target_date}] AS goals",
        id=member_id,
    )
    record = records[0]
    if record["total"] == 0:
        return None
    if record["profile"] is None:
        raise HTTPException(status_code=404, detail=f"Member {member_id!r} not found")
    try:
        goals = sorted(record["goals"], key=lambda g: (g["priority"], g["id"]))
        return MemberResponse(
            profile=MemberProfile.model_validate(record["profile"]),
            goals=[Goal.model_validate(goal) for goal in goals],
        )
    except ValidationError as exc:
        logger.error("Graph member %s failed validation: %s", member_id, exc)
        raise HTTPException(status_code=500, detail="Member data unavailable")


@router.get("/{member_id}", response_model=MemberResponse)
async def get_member(member_id: str, request: Request) -> MemberResponse:
    """Return the profile header and goals for a member.

    Reads the Member Context graph (KG 2). When Neo4j is unreachable or the
    graph has not been built yet, falls back to the ``member-context.json``
    seed file so the dashboard stays usable before/without ingest.

    Args:
        member_id: Member identifier, e.g. ``mbr_01HX9JORDAN``.
        request: Current request; carries the app-lifetime Neo4j driver.

    Returns:
        MemberResponse with the member's profile fields and goals.

    Raises:
        HTTPException: 404 if ``member_id`` does not match a known member;
            500 if neither the graph nor the seed file can serve it.
    """
    driver: AsyncDriver = request.app.state.neo4j
    try:
        # The timeout keeps a down/unreachable Neo4j from stalling the
        # dashboard for the driver's full connection-retry window.
        async with asyncio.timeout(GRAPH_READ_TIMEOUT_SECONDS):
            member = await _member_from_graph(driver, member_id)
    except (ServiceUnavailable, Neo4jError, OSError, TimeoutError) as exc:
        logger.warning("Neo4j unavailable for member read, using seed file: %s", exc)
        member = None
    if member is None:
        return _member_from_seed(member_id)
    return member
