from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from neo4j import AsyncDriver
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import Link, SpanKind, Status, StatusCode
from opentelemetry.util.types import Attributes

from app.config import settings
from app.observability.exporter import SqlSpanExporter
from app.observability.store import TraceStore

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("kg-coach")

# Health checks run a Cypher query every 5 seconds from both `make up` and the
# Compose healthcheck. With no HTTP span above them those queries would each
# become their own root trace -- roughly 17k junk traces a day, burying the
# handful a reviewer actually triggered.
_DROPPED_ROOT_SPAN_NAMES = frozenset({"neo4j.query"})

# Path prefixes that get no request span at all. Anything under one of these is
# therefore parentless, which is what makes the sampler above able to drop it.
#
# /api/traces is the trace store's own read API. Tracing it is a feedback loop:
# every render of the Traces page mints two more traces, which inflate the
# figures on that same page and bury the runs worth looking at. Excluding the
# telemetry read path is standard for the same reason exporters never trace
# their own exports.
_UNTRACED_PATH_PREFIXES = ("/api/health", "/api/traces")

# Module-level singletons. OpenTelemetry's `set_tracer_provider` is set-once:
# a second call logs a warning and keeps the first provider, so a second
# configure would silently send spans to a provider nothing is reading.
_provider: TracerProvider | None = None
_store: TraceStore | None = None


class DropRootNoiseSampler(Sampler):
    """Drops spans that are known noise *and* have no parent.

    Only root spans: `ParentBased` delegates anything with a sampled parent to
    ALWAYS_ON, so a `neo4j.query` inside an agent run survives while the
    identically-named health-check query does not.

    Dropping at the sampler rather than filtering in the exporter matters:
    a dropped span is non-recording, and Pydantic AI checks `is_recording()`
    before serialising prompts and tool definitions. Filtering later would pay
    the whole serialisation cost and then throw the result away.
    """

    def __init__(self, dropped_names: frozenset[str]) -> None:
        """Configure the sampler.

        Args:
            dropped_names: Span names to drop when they appear as trace roots.
        """
        self._dropped_names = dropped_names

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: trace.TraceState | None = None,
    ) -> SamplingResult:
        """Decide whether to record a root span.

        Note that `attributes` holds only what was passed at span start, never
        anything set later, so this must never key on a late attribute.

        Args:
            parent_context: Unused; ParentBased has already established that
                there is no sampled parent.
            trace_id: The trace this span belongs to.
            name: The span name.
            kind: The OpenTelemetry span kind.
            attributes: Attributes supplied at span start.
            links: Span links.
            trace_state: Inbound trace state.

        Returns:
            A DROP result for known noise, otherwise record and sample.
        """
        if name in self._dropped_names:
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        """Describe the sampler for diagnostics."""
        return f"DropRootNoiseSampler({sorted(self._dropped_names)})"


def configure_observability() -> TraceStore | None:
    """Install the tracing pipeline and instrument every agent.

    Idempotent: calling it again returns the store built the first time,
    because the global tracer provider can only be set once per process.

    Returns:
        The trace store to read from, or None when tracing is disabled.
    """
    global _provider, _store

    if _store is not None:
        return _store
    if not settings.obs_enabled:
        logger.info("Tracing disabled (OBS_ENABLED=false)")
        return None

    from pydantic_ai import Agent, InstrumentationSettings

    store = TraceStore(settings.obs_database_url)
    # A database that is down at startup must not stop the API from booting;
    # the exporter retries the schema on its next batch.
    store.ensure_schema()

    exporter = SqlSpanExporter(
        store,
        capture_content=settings.obs_capture_content,
        content_max_chars=settings.obs_content_max_chars,
        retention=timedelta(days=settings.obs_retention_days),
        sweep_interval_seconds=settings.obs_sweep_interval_seconds,
    )
    provider = TracerProvider(
        resource=Resource.create({"service.name": "kg-coach-api"}),
        sampler=ParentBased(root=DropRootNoiseSampler(_DROPPED_ROOT_SPAN_NAMES)),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Pydantic AI's native spans: agent runs, model requests, tool calls.
    # `include_model_request_parameters=False` drops a full serialisation of
    # every tool definition from each model-request span; the content that is
    # actually useful (prompts, completions, tool arguments) is kept and
    # truncated at ingest.
    Agent.instrument_all(
        InstrumentationSettings(include_model_request_parameters=False)
    )

    _provider, _store = provider, store
    logger.info("Tracing enabled -> %s", _redacted(settings.obs_database_url))
    return store


def shutdown_observability() -> None:
    """Flush pending spans on application shutdown.

    Deliberately does not shut the provider down or dispose the store. The
    provider is process-global and set-once, so tearing it down would leave a
    later `configure_observability` unable to reinstall one -- which is
    exactly what happens across tests, where each TestClient runs lifespan.
    """
    if _provider is not None:
        _provider.force_flush()


def get_store() -> TraceStore | None:
    """Return the configured trace store, if tracing is on."""
    return _store


class RequestSpanMiddleware:
    """Opens one span per HTTP request, so a request is one trace.

    Without this, a single `POST /api/workout` produces around thirty
    unrelated root traces: the agent runs plus one per resolver, safety, and
    member-context query, since those run outside any agent. Grouping is the
    entire point of a trace store.

    Pure ASGI rather than Starlette's `BaseHTTPMiddleware`, which breaks
    context propagation. The application coroutine only returns once the whole
    response body has been sent, so the span still covers the copilot's
    server-sent-event stream even though the agent run outlives the handler.
    """

    def __init__(
        self,
        app: Any,
        *,
        untraced_path_prefixes: tuple[str, ...] = _UNTRACED_PATH_PREFIXES,
        tracer: trace.Tracer | None = None,
    ):
        """Wrap an ASGI application.

        Args:
            app: The downstream ASGI application.
            untraced_path_prefixes: Path prefixes that produce no span at all.
            tracer: Tracer to open request spans on. Defaults to the module
                tracer, which resolves to the global provider.
        """
        self._app = app
        self._untraced_path_prefixes = untraced_path_prefixes
        self._tracer = tracer or _tracer

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Handle one ASGI event, tracing HTTP requests.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != "http" or scope["path"].startswith(
            self._untraced_path_prefixes
        ):
            await self._app(scope, receive, send)
            return

        method: str = scope["method"]
        span = self._tracer.start_span(
            f"{method} {scope['path']}",
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": scope["path"]},
        )

        async def send_with_status(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                span.set_attribute("http.response.status_code", status_code)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))
            await send(message)

        # end_on_exit=False so the span is closed exactly once, in the finally
        # below, whether the response completed or the client disconnected.
        with trace.use_span(span, end_on_exit=False, record_exception=False):
            try:
                await self._app(scope, receive, send_with_status)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                _label_route(span, scope)
                span.end()


def _label_route(span: trace.Span, scope: dict[str, Any]) -> None:
    """Attach the matched route template and member id to a request span.

    Read after the application has run, because routing is what populates
    them. Using the template rather than the raw path keeps `/api/members/{id}`
    from becoming one distinct span name per member.

    Args:
        span: The request span, not yet ended.
        scope: The ASGI scope, mutated in place by Starlette's router.
    """
    route = scope.get("route")
    path_format = getattr(route, "path", None)
    if path_format:
        span.set_attribute("http.route", path_format)
        span.update_name(f"{scope['method']} {path_format}")
    member_id = (scope.get("path_params") or {}).get("member_id")
    if member_id:
        span.set_attribute("kg_coach.member_id", str(member_id))


class TracedAsyncDriver:
    """Neo4j driver proxy that wraps ``execute_query`` in a span.

    Every tool-layer read (resolver passes, safety traversal, member-context
    retrieval) goes through ``execute_query``, so this one hook makes graph
    traversals visible inside agent traces. All other attributes delegate to
    the real driver.
    """

    def __init__(self, driver: AsyncDriver) -> None:
        """Wrap a real driver.

        Args:
            driver: The Neo4j async driver to delegate to.
        """
        self._driver = driver

    async def execute_query(self, query: str, **kwargs: Any) -> Any:
        """Run a Cypher query, recording it as a span.

        Args:
            query: The Cypher statement.
            **kwargs: Parameters and options passed through to the driver.

        Returns:
            Whatever the underlying driver returns.

        Raises:
            Exception: Anything the driver raises, after recording it.
        """
        with _tracer.start_as_current_span(
            "neo4j.query",
            attributes={
                "db.system": "neo4j",
                # The first line identifies the traversal without shipping
                # full parameterised statements into the trace store.
                "db.statement.summary": query.strip().splitlines()[0][:200],
            },
        ) as span:
            try:
                result = await self._driver.execute_query(query, **kwargs)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            span.set_attribute("db.response.returned_rows", len(result[0]))
            return result

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else to the wrapped driver."""
        return getattr(self._driver, name)


def _redacted(database_url: str) -> str:
    """Strip credentials out of a database URL before logging it.

    Args:
        database_url: The SQLAlchemy URL.

    Returns:
        The URL with any user:password section replaced.
    """
    if "@" not in database_url:
        return database_url
    scheme, _, remainder = database_url.partition("://")
    return f"{scheme}://***@{remainder.partition('@')[2]}"
