"""``POST /api/copilot/{member_id}`` — AG-UI streaming endpoint (issue #10).

The browser speaks the AG-UI protocol: it POSTs a ``RunAgentInput`` carrying
the whole conversation (which is what makes multi-turn follow-ups work) and
receives Server-Sent Events — streamed text, tool calls, and the typed
``CopilotAnswer`` output.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from neo4j import AsyncDriver
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic_ai.ui.ag_ui import AGUIAdapter

from app.agents.copilot import (
    ALL_SECTIONS,
    ContextSlice,
    CopilotDeps,
    SectionName,
    copilot_agent,
    fetch_context_slice,
)
from app.agents.model import build_model, build_model_settings
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["copilot"])


@router.get("/members/{member_id}/context", response_model=ContextSlice)
async def member_context_slice(
    member_id: str,
    request: Request,
    sections: list[SectionName] = Query(default=list(ALL_SECTIONS)),
) -> ContextSlice:
    """Read sections of the member-context graph directly (no LLM).

    Backs the copilot panel's morning-brief and chat-history surfaces — the
    same retrieval the agent's ``member_context`` tool uses.

    Raises:
        HTTPException: 404 for an unknown member, 503 when Neo4j is down.
    """
    driver: AsyncDriver = request.app.state.neo4j
    try:
        return await fetch_context_slice(driver, member_id, list(sections))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ServiceUnavailable, Neo4jError, OSError) as exc:
        logger.error("Context slice read failed: %s", exc)
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")


@router.post("/copilot/{member_id}")
async def copilot_run(member_id: str, request: Request) -> Response:
    """Run one copilot turn for a member, streaming AG-UI events back.

    Raises:
        HTTPException: 503 when the model API key or Neo4j is unavailable.
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured; the copilot needs it",
        )
    driver: AsyncDriver = request.app.state.neo4j
    try:
        return await AGUIAdapter.dispatch_request(
            request,
            agent=copilot_agent,
            deps=CopilotDeps(driver=driver, member_id=member_id),
            model=build_model(),
            model_settings=build_model_settings(effort="low", max_tokens=4096),
        )
    except (ServiceUnavailable, Neo4jError, OSError) as exc:
        logger.error("Copilot run failed on Neo4j: %s", exc)
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
