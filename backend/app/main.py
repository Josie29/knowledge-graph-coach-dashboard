from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import BaseModel

from app.config import settings
from app.copilot import router as copilot_router
from app.graph import router as graph_router
from app.members import router as members_router
from app.observability import TracedAsyncDriver, configure_observability
from app.workouts import router as workouts_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the Neo4j driver for the app's lifetime and close it on shutdown.

    Yields:
        None. The driver is stored on ``app.state.neo4j``.
    """
    configure_observability()
    driver: AsyncDriver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    # The proxy adds an OTel span per query; spans are no-ops when tracing
    # is not configured, so this wrap is unconditional.
    app.state.neo4j = TracedAsyncDriver(driver)
    yield
    await driver.close()


app = FastAPI(title="KG Coach API", lifespan=lifespan)
app.include_router(members_router)
app.include_router(workouts_router)
app.include_router(copilot_router)
app.include_router(graph_router)


class HealthResponse(BaseModel):
    """Health check result for the API and its Neo4j dependency."""

    status: str
    neo4j: str
    ai_enabled: bool
    model: str
    exercises: int
    members: int


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
        )
    return HealthResponse(
        status="ok",
        neo4j="connected",
        ai_enabled=ai_enabled,
        model=settings.anthropic_model,
        exercises=records[0]["exercises"],
        members=records[0]["members"],
    )
