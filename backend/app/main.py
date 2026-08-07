import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.copilot import router as copilot_router
from app.graph import router as graph_router
from app.members import router as members_router
from app.observability import (
    RequestSpanMiddleware,
    TraceStore,
    TracedAsyncDriver,
    configure_observability,
    shutdown_observability,
)
from app.observability import router as traces_router
from app.workouts import router as workouts_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the Neo4j driver and the trace store for the app's lifetime.

    Yields:
        None. The driver is stored on ``app.state.neo4j`` and the trace store
        on ``app.state.trace_store``.
    """
    app.state.trace_store = configure_observability()
    driver: AsyncDriver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    # The proxy adds a span per query; spans are no-ops when tracing is not
    # configured, so this wrap is unconditional.
    app.state.neo4j = TracedAsyncDriver(driver)
    yield
    shutdown_observability()
    await driver.close()


app = FastAPI(title="KG Coach API", lifespan=lifespan)
# Outermost, so one request is one trace: the agent runs and the resolver,
# safety, and member-context queries all nest under a single request span
# instead of becoming a few dozen unrelated root traces.
app.add_middleware(RequestSpanMiddleware)
app.include_router(members_router)
app.include_router(workouts_router)
app.include_router(copilot_router)
app.include_router(graph_router)
app.include_router(traces_router)


class HealthResponse(BaseModel):
    """Health check result for the API and its Neo4j dependency."""

    status: str
    neo4j: str
    ai_enabled: bool
    model: str
    exercises: int
    members: int
    tracing_enabled: bool
    traces_recorded: int


# Label counts come from Neo4j's store statistics, so this stays O(1) even
# though the health endpoint is polled on a loop by `make up` and Compose.
_GRAPH_COUNTS_QUERY = (
    "RETURN count{(:Exercise)} AS exercises, count{(:Member)} AS members"
)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report API liveness, Neo4j connectivity, and what the graph holds.

    The startup banner (`make up`) reads this to decide when the stack is
    actually serving and whether the AI features are configured.

    Returns:
        HealthResponse with ``status`` \"ok\" when Neo4j is reachable and the
        graph is loaded, \"degraded\" otherwise. Counts are 0 when degraded.
    """
    driver: AsyncDriver = app.state.neo4j
    ai_enabled = bool(settings.anthropic_api_key)
    trace_store: TraceStore | None = app.state.trace_store
    traces_recorded = await _count_traces(trace_store)
    try:
        await driver.verify_connectivity()
        records, _, _ = await driver.execute_query(_GRAPH_COUNTS_QUERY)
    except (ServiceUnavailable, Neo4jError, OSError) as exc:
        return HealthResponse(
            status="degraded",
            neo4j=f"unreachable: {exc}",
            ai_enabled=ai_enabled,
            model=settings.anthropic_model,
            exercises=0,
            members=0,
            tracing_enabled=trace_store is not None,
            traces_recorded=traces_recorded,
        )
    return HealthResponse(
        status="ok",
        neo4j="connected",
        ai_enabled=ai_enabled,
        model=settings.anthropic_model,
        exercises=records[0]["exercises"],
        members=records[0]["members"],
        tracing_enabled=trace_store is not None,
        traces_recorded=traces_recorded,
    )


async def _count_traces(trace_store: TraceStore | None) -> int:
    """Count stored traces for the startup banner, tolerating an outage.

    Args:
        trace_store: The configured store, or None when tracing is disabled.

    Returns:
        The number of distinct traces, or 0 if tracing is off or the store
        cannot be read. Health must never fail because of diagnostics.
    """
    if trace_store is None:
        return 0
    try:
        return await run_in_threadpool(trace_store.count_traces)
    except SQLAlchemyError as exc:
        logger.warning("Could not count traces for health: %s", exc)
        return 0
