"""``POST /api/workout`` — the workout generator endpoint (issue #8)."""

import logging

from fastapi import APIRouter, HTTPException, Request
from neo4j import AsyncDriver
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from app.agents.workout import WorkoutPlan, WorkoutRequest, generate_workout
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["workouts"])


@router.post("/workout", response_model=WorkoutPlan)
async def create_workout(request: Request, body: WorkoutRequest) -> WorkoutPlan:
    """Generate (or adjust) a workout plan for a member.

    Send the previous response's ``constraints_used`` back as
    ``prior_constraints`` together with a follow-up message to adjust the
    plan — the constraint set merges and the safety traversal re-runs.

    Raises:
        HTTPException: 503 when Neo4j or the model API is unavailable, 422
            when the constraints leave an empty exercise pool or leave the
            planner unable to fill the requested time window, 500 otherwise.
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured; the generator needs it",
        )
    driver: AsyncDriver = request.app.state.neo4j
    try:
        return await generate_workout(driver, body)
    except (ServiceUnavailable, Neo4jError, OSError) as exc:
        logger.error("Workout generation failed on Neo4j: %s", exc)
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
