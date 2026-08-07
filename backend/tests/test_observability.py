"""Tests for the local trace store.

The critical path is span ingest: everything the Traces page shows is derived
from `ingest.map_span`, so a silent classification or token-accounting change
would empty or falsify the page without breaking anything else. The most
valuable test here drives a real Pydantic AI agent through a real tracer, so an
upgrade that renames spans fails the suite instead of the dashboard.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased
from opentelemetry.trace import (
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)

from app.observability.exporter import SqlSpanExporter
from app.observability.ingest import SpanCategory, SpanStatus, classify
from app.observability.setup import DropRootNoiseSampler
from app.observability.store import TraceStore, spans_table

_APP_PACKAGE = Path(__file__).resolve().parents[1] / "app"


def make_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    trace_id: int = 0x1234,
    span_id: int = 0x1,
    parent_span_id: int | None = None,
    status: Status | None = None,
    duration_ms: float = 10.0,
    start_ns: int | None = None,
) -> ReadableSpan:
    """Build a finished span without running any instrumentation.

    Args:
        name: The span name.
        attributes: OpenTelemetry attributes to attach.
        trace_id: Trace the span belongs to.
        span_id: This span's id.
        parent_span_id: Parent span id, or None for a root span.
        status: The span's status; unset by default.
        duration_ms: How long the span lasted.
        start_ns: Explicit start time in epoch nanoseconds.

    Returns:
        A ReadableSpan shaped like one the SDK would export.
    """
    start = start_ns if start_ns is not None else time.time_ns()
    return ReadableSpan(
        name=name,
        context=SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        parent=(
            SpanContext(
                trace_id=trace_id,
                span_id=parent_span_id,
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            if parent_span_id is not None
            else None
        ),
        attributes=attributes or {},
        status=status or Status(StatusCode.UNSET),
        kind=SpanKind.INTERNAL,
        start_time=start,
        end_time=start + int(duration_ms * 1_000_000),
        resource=Resource.create({}),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    """A trace store backed by a throwaway SQLite file."""
    trace_store = TraceStore(f"sqlite+pysqlite:///{tmp_path / 'traces.db'}")
    trace_store.create_schema()
    yield trace_store
    trace_store.dispose()


@pytest.fixture
def exporter(store: TraceStore) -> SqlSpanExporter:
    """An exporter writing to the temporary store, content capture on."""
    return SqlSpanExporter(
        store,
        capture_content=True,
        content_max_chars=2000,
        retention=timedelta(days=7),
        sweep_interval_seconds=3600,
    )


# --- classification -------------------------------------------------------


def test_span_categories_come_from_the_operation_name_not_the_span_name() -> None:
    """Catches the bug where an agent-framework upgrade renames its spans and
    every row on the Traces page silently becomes "other".

    Pydantic AI has already done this once: `agent run` became
    `invoke_agent {name}` and `running tool` became `execute_tool {name}`
    between instrumentation v2 and v5.
    """
    assert (
        classify({"gen_ai.operation.name": "chat"}, "some future span name")
        is SpanCategory.LLM
    )
    assert (
        classify({"gen_ai.operation.name": "invoke_agent"}, "whatever")
        is SpanCategory.AGENT
    )
    assert (
        classify({"gen_ai.operation.name": "execute_tool"}, "whatever")
        is SpanCategory.TOOL
    )
    assert classify({"db.system": "neo4j"}, "neo4j.query") is SpanCategory.DB
    # A framework that sets the semantic-convention attributes but no
    # operation name still classifies rather than falling through to "other".
    assert classify({"gen_ai.tool.name": "member_context"}, "x") is SpanCategory.TOOL
    assert classify({"gen_ai.request.model": "claude"}, "x") is SpanCategory.LLM
    assert classify({}, "unrecognised") is SpanCategory.OTHER


def test_real_pydantic_ai_spans_are_classified(tmp_path: Path) -> None:
    """Catches the bug where the installed agent framework emits attribute
    keys this repo no longer recognises, so the Traces page shows a run as an
    untyped blob with no model, no tool, and no token counts.

    Drives a real agent through a real tracer with no API key and no network.
    """
    from pydantic_ai import Agent, InstrumentationSettings
    from pydantic_ai.capabilities.instrumentation import Instrumentation
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    trace_store = TraceStore(f"sqlite+pysqlite:///{tmp_path / 'agent.db'}")
    trace_store.create_schema()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            SqlSpanExporter(
                trace_store,
                capture_content=True,
                content_max_chars=2000,
                retention=timedelta(days=7),
                sweep_interval_seconds=3600,
            )
        )
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {"topic": "squat"})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(respond),
        name="test-agent",
        capabilities=[
            Instrumentation(
                settings=InstrumentationSettings(tracer_provider=provider)
            )
        ],
    )

    @agent.tool_plain
    def lookup(topic: str) -> str:
        """Return a canned fact about a topic."""
        return f"fact about {topic}"

    agent.run_sync("tell me about squats")
    provider.force_flush()

    traces = trace_store.list_traces()
    assert len(traces) == 1
    categories = {span.category for span in trace_store.get_trace(traces[0].trace_id).spans}
    assert SpanCategory.AGENT in categories
    assert SpanCategory.LLM in categories
    assert SpanCategory.TOOL in categories
    trace_store.dispose()


# --- token and cost accounting -------------------------------------------


def test_token_totals_ignore_the_agent_runs_aggregated_usage(
    store: TraceStore, exporter: SqlSpanExporter
) -> None:
    """Catches the bug where the trace's token and cost figures read exactly
    double, because the agent-run span repeats its children's usage under
    `gen_ai.aggregated_usage.*` and both get summed.
    """
    exporter.export(
        [
            make_span(
                "invoke_agent planner",
                span_id=0x1,
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "planner",
                    "gen_ai.aggregated_usage.input_tokens": 300,
                    "gen_ai.aggregated_usage.output_tokens": 40,
                },
            ),
            make_span(
                "chat claude-haiku-4-5",
                span_id=0x2,
                parent_span_id=0x1,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "claude-haiku-4-5",
                    "gen_ai.usage.input_tokens": 300,
                    "gen_ai.usage.output_tokens": 40,
                    "operation.cost": 0.0025,
                },
            ),
        ]
    )

    summary = store.list_traces()[0]
    assert summary.input_tokens == 300
    assert summary.output_tokens == 40
    assert summary.cost_micro_usd == 2500


def test_cost_is_read_from_the_frameworks_own_price_calculation(
    store: TraceStore, exporter: SqlSpanExporter
) -> None:
    """Catches the bug where an unpriced model silently reports $0.00 instead
    of nothing, making a cost dashboard confidently wrong.
    """
    exporter.export(
        [
            make_span(
                "chat mystery-model",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "mystery-model",
                    "gen_ai.usage.input_tokens": 100,
                },
            )
        ]
    )
    assert store.list_traces()[0].cost_micro_usd == 0


# --- content capture ------------------------------------------------------


def test_prompt_content_is_truncated_to_the_configured_cap(store: TraceStore) -> None:
    """Catches the bug where one long copilot conversation writes megabytes
    per span and fills the disk, because the message history grows every turn
    and is repeated on each model request.
    """
    exporter = SqlSpanExporter(
        store,
        capture_content=True,
        content_max_chars=50,
        retention=timedelta(days=7),
        sweep_interval_seconds=3600,
    )
    exporter.export(
        [
            make_span(
                "chat claude",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.input.messages": "x" * 10_000,
                },
            )
        ]
    )

    span = store.get_trace(store.list_traces()[0].trace_id).spans[0]
    assert span.input_preview is not None
    assert len(span.input_preview) == 50
    assert span.input_truncated is True


def test_content_capture_can_be_turned_off(store: TraceStore) -> None:
    """Catches the bug where prompt text is still written after an operator
    sets OBS_CAPTURE_CONTENT=false to keep member data out of the store.
    """
    exporter = SqlSpanExporter(
        store,
        capture_content=False,
        content_max_chars=2000,
        retention=timedelta(days=7),
        sweep_interval_seconds=3600,
    )
    exporter.export(
        [
            make_span(
                "chat claude",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.input.messages": "a member's private notes",
                },
            )
        ]
    )

    span = store.get_trace(store.list_traces()[0].trace_id).spans[0]
    assert span.input_preview is None
    assert "a member's private notes" not in str(span.attributes)


def test_message_content_is_not_stored_twice(
    store: TraceStore, exporter: SqlSpanExporter
) -> None:
    """Catches the bug where the full untruncated message history survives in
    the attributes blob, making the truncation cap decorative.
    """
    exporter.export(
        [
            make_span(
                "chat claude",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.input.messages": "y" * 10_000,
                    "server.address": "api.anthropic.com",
                },
            )
        ]
    )

    span = store.get_trace(store.list_traces()[0].trace_id).spans[0]
    assert "gen_ai.input.messages" not in span.attributes
    # Attributes that are neither content nor a promoted column survive.
    assert span.attributes["server.address"] == "api.anthropic.com"


# --- resilience -----------------------------------------------------------


def test_export_reports_failure_instead_of_raising_when_the_store_is_down(
    tmp_path: Path,
) -> None:
    """Catches the bug where a Postgres restart turns every span export into
    an unhandled exception, burying the API logs in tracebacks.
    """
    unreachable = TraceStore(
        "postgresql+psycopg://nobody:nobody@127.0.0.1:1/nothing"
    )
    exporter = SqlSpanExporter(
        unreachable,
        capture_content=True,
        content_max_chars=2000,
        retention=timedelta(days=7),
        sweep_interval_seconds=3600,
    )

    result = exporter.export([make_span("chat claude")])

    assert result is SpanExportResult.FAILURE


def test_a_replayed_batch_does_not_double_the_totals(
    store: TraceStore, exporter: SqlSpanExporter
) -> None:
    """Catches the bug where a retried export doubles a trace's token and cost
    figures because the same span ids are inserted twice.
    """
    batch = [
        make_span(
            "chat claude",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": 100,
                "operation.cost": 0.001,
            },
        )
    ]
    exporter.export(batch)
    exporter.export(batch)

    summary = store.list_traces()[0]
    assert summary.span_count == 1
    assert summary.input_tokens == 100
    assert summary.cost_micro_usd == 1000


def test_a_failed_span_marks_the_whole_trace_as_failed(
    store: TraceStore, exporter: SqlSpanExporter
) -> None:
    """Catches the bug where a run whose tool raised looks identical to a
    successful one in the traces list, making "why did that fail?" unanswerable.
    """
    exporter.export(
        [
            make_span("invoke_agent copilot", span_id=0x1),
            make_span(
                "execute_tool member_context",
                span_id=0x2,
                parent_span_id=0x1,
                attributes={"gen_ai.operation.name": "execute_tool"},
                status=Status(StatusCode.ERROR, "graph unreachable"),
            ),
        ]
    )

    summary = store.list_traces()[0]
    assert summary.status is SpanStatus.ERROR
    assert summary.error_count == 1


def test_a_trace_is_readable_before_its_root_span_arrives(
    store: TraceStore, exporter: SqlSpanExporter
) -> None:
    """Catches the bug where an in-flight or crashed run vanishes from the
    traces list, because a parent span always ends after its children and may
    never arrive at all.
    """
    exporter.export(
        [
            make_span(
                "chat claude",
                span_id=0x2,
                parent_span_id=0x1,
                attributes={"gen_ai.operation.name": "chat"},
            )
        ]
    )

    summary = store.list_traces()[0]
    assert summary.span_count == 1
    assert summary.name == "(incomplete trace)"


# --- noise filtering ------------------------------------------------------


def test_health_check_graph_queries_are_dropped_but_agent_ones_are_kept() -> None:
    """Catches the bug where the Traces page drowns in health checks: the
    Compose healthcheck and `make up` poll /api/health every five seconds, and
    each poll runs a Cypher query that would otherwise become its own trace.

    Also catches over-correcting: the identically named graph traversals
    inside an agent run are the whole point of the trace and must survive.
    """
    provider = TracerProvider(
        sampler=ParentBased(root=DropRootNoiseSampler(frozenset({"neo4j.query"})))
    )
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("neo4j.query") as health_query:
        assert health_query.is_recording() is False

    with tracer.start_as_current_span("POST /api/workout"):
        with tracer.start_as_current_span("neo4j.query") as tool_query:
            assert tool_query.is_recording() is True


def test_retention_sweep_deletes_expired_spans(store: TraceStore) -> None:
    """Catches the bug where a long-running demo box fills its disk because
    nothing ever removes old traces.
    """
    old = time.time_ns() - int(timedelta(days=30).total_seconds() * 1_000_000_000)
    exporter = SqlSpanExporter(
        store,
        capture_content=True,
        content_max_chars=2000,
        retention=timedelta(days=7),
        sweep_interval_seconds=0,
    )
    exporter.export([make_span("chat old", span_id=0x1, start_ns=old)])
    exporter.export([make_span("chat new", span_id=0x2, trace_id=0x999)])

    remaining = store.list_traces()
    assert [summary.trace_id for summary in remaining] == [f"{0x999:032x}"]


def test_one_api_request_becomes_one_trace_and_health_becomes_none() -> None:
    """Catches the two bugs that make a trace store useless here.

    Without a request span, a single POST /api/workout produces around thirty
    unrelated root traces -- the agent runs plus one per resolver, safety, and
    member-context query -- so nothing shows the story of one request. And
    without the health exclusion, the five-second Compose healthcheck buries
    those traces under thousands of its own.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.observability import shutdown_observability

    with TestClient(app) as client:
        trace_store: TraceStore = app.state.trace_store
        with trace_store.engine.begin() as conn:
            conn.execute(sa.delete(spans_table))

        client.get("/api/health")
        client.get("/api/members/mbr_01HX9JORDAN")
        shutdown_observability()

        traces = trace_store.list_traces()

    routes = [summary.route for summary in traces]
    assert routes == ["/api/members/{member_id}"]
    # The route template, not the raw path: otherwise every member would
    # produce a differently-named trace and grouping would be meaningless.
    assert traces[0].member_id == "mbr_01HX9JORDAN"


def test_the_request_span_covers_a_streamed_response() -> None:
    """Catches the bug where the copilot's spans land in their own trace.

    The copilot answers through a server-sent-event stream, so its agent run
    happens inside the response body, after the route handler has returned. A
    request span that closed when the handler returned would leave the model
    and tool calls parented to nothing, and the Traces page would show the
    request and the run as two unrelated traces.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from starlette.responses import StreamingResponse

    from app.observability.setup import RequestSpanMiddleware

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("test")

    streaming_app = FastAPI()
    streaming_app.add_middleware(RequestSpanMiddleware, tracer=tracer)

    @streaming_app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body():
            yield b"start\n"
            with tracer.start_as_current_span("invoke_agent copilot"):
                yield b"end\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    with TestClient(streaming_app) as client:
        client.get("/stream")

    spans = {span.name: span for span in memory.get_finished_spans()}
    request_span = spans["GET /stream"]
    agent_span = spans["invoke_agent copilot"]
    assert agent_span.parent is not None
    assert agent_span.parent.span_id == request_span.context.span_id
    assert request_span.end_time >= agent_span.end_time


# --- the seam ------------------------------------------------------------


def test_framework_specific_attribute_keys_stay_in_the_ingest_module() -> None:
    """Catches the bug that makes the next agent-framework swap a rewrite:
    `gen_ai.*` keys leaking into the store, exporter, API, or agent code, so
    porting means hunting them across the codebase instead of editing one file.
    """
    leaked = [
        path.relative_to(_APP_PACKAGE).as_posix()
        for path in _APP_PACKAGE.rglob("*.py")
        if path.name != "ingest.py" and "gen_ai." in path.read_text()
    ]
    assert leaked == []


def test_trace_reads_work_identically_on_a_second_dialect(
    store: TraceStore, exporter: SqlSpanExporter
) -> None:
    """Catches the bug where the store grows Postgres-only SQL and the
    documented "swap the URL" portability claim quietly stops being true.

    SQLite is the second dialect exercised here; the same statements run
    against Postgres in docker-compose.
    """
    exporter.export(
        [
            make_span(
                "chat claude",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.usage.input_tokens": 10,
                },
            )
        ]
    )

    stats = store.stats(window_hours=24)
    assert stats.trace_count == 1
    assert stats.llm_call_count == 1
    assert stats.total_tokens == 10
    with store.engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(spans_table)).scalar_one() == 1


def test_naive_and_aware_timestamps_round_trip_the_same(store: TraceStore) -> None:
    """Catches the bug where trace times are off by the local UTC offset on
    one database and correct on the other, because SQLite returns naive
    datetimes and Postgres returns aware ones.
    """
    exporter = SqlSpanExporter(
        store,
        capture_content=False,
        content_max_chars=100,
        retention=timedelta(days=7),
        sweep_interval_seconds=3600,
    )
    started = datetime.now(UTC)
    exporter.export([make_span("chat claude", start_ns=int(started.timestamp() * 1e9))])

    summary = store.list_traces()[0]
    assert summary.started_at.tzinfo is not None
    assert abs((summary.started_at - started).total_seconds()) < 1
