"""Shared fixtures for the backend test suite.

The resolver and safety-filter suites (issue #12) come in two layers:

- **Unit tests** exercise the pure-Python decision logic (rule evaluation,
  pass thresholds, Lucene escaping) with fakes — they always run, offline,
  with no Neo4j and no LLM.
- **Integration tests** run the same code against the real built graph and
  the reference member scenario. They need a reachable Neo4j with the KG
  loaded (``docker compose up neo4j`` + ``scripts/build_kg.py``) and skip
  with a clear message otherwise, so ``uv run pytest`` stays green offline.
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Iterator

import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase, GraphDatabase


@pytest.fixture(scope="session", autouse=True)
def isolated_trace_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point the trace store at a throwaway SQLite file for the whole suite.

    Every test that opens ``TestClient(app)`` runs the app's lifespan, which
    configures tracing. Without this the suite would write a real ``traces.db``
    into the repository and accumulate spans from test runs.
    """
    from app.config import settings

    database = tmp_path_factory.mktemp("trace-store") / "traces.db"
    settings.obs_database_url = f"sqlite+pysqlite:///{database}"
    yield


def _neo4j_uri() -> str:
    return os.environ.get("NEO4J_URI", "bolt://localhost:7687")


def _neo4j_auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "password"),
    )


@pytest.fixture(scope="session")
def graph_available() -> str:
    """Skip integration tests unless a built KG is reachable."""
    uri = _neo4j_uri()
    try:
        with GraphDatabase.driver(
            uri, auth=_neo4j_auth(), connection_timeout=3.0
        ) as driver:
            driver.verify_connectivity()
            with driver.session() as session:
                count = session.run(
                    "MATCH (e:Exercise) RETURN count(e) AS n"
                ).single()["n"]
    except Exception as exc:  # noqa: BLE001 - any failure means "not available"
        pytest.skip(f"Neo4j not reachable at {uri} ({type(exc).__name__}) — "
                    "integration tests skipped")
    if count != 50:
        pytest.skip(
            f"graph at {uri} holds {count} exercises, expected 50 — "
            "run scripts/build_kg.py first"
        )
    return uri


@pytest.fixture
async def graph_driver(graph_available: str) -> AsyncIterator[AsyncDriver]:
    driver = AsyncGraphDatabase.driver(graph_available, auth=_neo4j_auth())
    yield driver
    await driver.close()
