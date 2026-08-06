"""Langfuse tracing via OpenTelemetry (issue #13).

Pydantic AI emits OTel spans natively for every agent run, model request,
and tool call; this module points those spans at Langfuse's OTLP endpoint
and adds spans for the Neo4j queries in the tool layer, so a single trace
shows the full story of one request: agent run → LLM calls → resolver /
safety-tool calls → the graph traversals underneath them.

Decision (docs/tech-stack.md): **Langfuse Cloud free tier**, not self-hosted
in Compose. Langfuse v3 self-hosting needs ClickHouse + Postgres + Redis +
MinIO — four extra containers that blow the repo's ~1.2 GB memory budget —
while the cloud free tier is zero-infra and covers a take-home many times
over. Tracing is opt-in: without ``LANGFUSE_PUBLIC_KEY``/``SECRET_KEY`` the
app runs exactly as before (OTel spans are non-recording no-ops).

Viewing a trace (reviewer note): create a free account at
https://cloud.langfuse.com, create a project, put its public/secret keys in
``.env`` (``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``; EU region hosts
keep the default ``LANGFUSE_HOST``, US region sets
``https://us.cloud.langfuse.com``), restart the stack, then generate one
workout and ask the copilot one question. Each appears in Langfuse →
Traces as ``agent run`` spans ("constraint-extractor" / "workout-planner" /
"member-copilot") with the model requests, tool calls, and ``neo4j.query``
spans nested inside.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from neo4j import AsyncDriver
from opentelemetry import trace

from app.config import settings

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("kg-coach.neo4j")


def configure_observability() -> bool:
    """Set up the OTLP→Langfuse pipeline and instrument all agents.

    Returns:
        True when tracing was configured; False when Langfuse keys are not
        set (the app then runs with no-op spans).
    """
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.info("Langfuse keys not set; tracing disabled")
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from pydantic_ai import Agent

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {auth}"},
    )
    provider = TracerProvider(
        resource=Resource.create({"service.name": "kg-coach-api"})
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # Pydantic AI's native spans: agent runs, model requests, tool calls.
    Agent.instrument_all()
    logger.info("Langfuse tracing enabled → %s", settings.langfuse_host)
    return True


class TracedAsyncDriver:
    """Neo4j driver proxy that wraps ``execute_query`` in an OTel span.

    Every tool-layer read (resolver passes, safety traversal, member-context
    retrieval) goes through ``execute_query``, so this one hook makes graph
    traversals visible inside agent traces. All other attributes delegate to
    the real driver; with tracing disabled the spans are non-recording.
    """

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def execute_query(self, query: str, **kwargs: Any) -> Any:
        with _tracer.start_as_current_span(
            "neo4j.query",
            attributes={
                "db.system": "neo4j",
                # First line is enough to identify the traversal in a trace
                # without shipping full parameterised statements.
                "db.statement.summary": query.strip().splitlines()[0][:200],
            },
        ) as span:
            result = await self._driver.execute_query(query, **kwargs)
            span.set_attribute("db.response.returned_rows", len(result[0]))
            return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)
