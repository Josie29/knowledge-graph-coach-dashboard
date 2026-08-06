import json
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/members", tags=["members"])


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


@router.get("/{member_id}", response_model=MemberResponse)
def get_member(member_id: str) -> MemberResponse:
    """Return the profile header and goals for a member.

    Currently backed by the ``data/member-context.json`` seed file, which
    holds a single member; a later issue swaps this for the knowledge graph.

    Args:
        member_id: Member identifier, e.g. ``mbr_01HX9JORDAN``.

    Returns:
        MemberResponse with the member's profile fields and goals.

    Raises:
        HTTPException: 404 if ``member_id`` does not match a known member;
            500 if the seed file is missing or malformed.
    """
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
