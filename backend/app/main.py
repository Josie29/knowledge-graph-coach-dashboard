from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import BaseModel

from app.config import settings
from app.members import router as members_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the Neo4j driver for the app's lifetime and close it on shutdown.

    Yields:
        None. The driver is stored on ``app.state.neo4j``.
    """
    driver: AsyncDriver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    app.state.neo4j = driver
    yield
    await driver.close()


app = FastAPI(title="KG Coach API", lifespan=lifespan)
app.include_router(members_router)


class HealthResponse(BaseModel):
    """Health check result for the API and its Neo4j dependency."""

    status: str
    neo4j: str


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report API liveness and Neo4j connectivity.

    Returns:
        HealthResponse with ``status`` \"ok\" when Neo4j is reachable,
        \"degraded\" otherwise.
    """
    driver: AsyncDriver = app.state.neo4j
    try:
        await driver.verify_connectivity()
    except (ServiceUnavailable, Neo4jError, OSError) as exc:
        return HealthResponse(status="degraded", neo4j=f"unreachable: {exc}")
    return HealthResponse(status="ok", neo4j="connected")
