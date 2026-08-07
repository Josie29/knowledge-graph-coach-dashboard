from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

# ==========================================================================
# THE SEAM
#
# This is the only module in the app that knows how the agent framework names
# and labels its spans. Everything downstream -- store, exporter, read API,
# Traces page -- speaks `SpanRecord` and is framework-agnostic.
#
# Porting to a different agent framework (LangGraph, the raw Anthropic SDK,
# anything else) means editing the constants below and, at most, the extract
# helpers. `tests/test_observability.py` enforces this by asserting that the
# string "gen_ai." appears in no other module under app/.
#
# Two rules keep it that way:
#   1. Classify on `gen_ai.operation.name`, never on span names. Operation
#      names are OpenTelemetry semantic convention; span names are framework
#      flavour. Pydantic AI renamed `agent run` -> `invoke_agent {name}` and
#      `running tool` -> `execute_tool {name}` between instrumentation v2 and
#      v5, which is exactly the breakage this rule avoids.
#   2. Promote anything the UI filters or groups by into a real column. The
#      `attributes` blob is for display only and is never queried in SQL --
#      that is what keeps Postgres and SQLite interchangeable.
# ==========================================================================

# --- Port point: OpenTelemetry GenAI semantic conventions (cross-framework)
_OPERATION = "gen_ai.operation.name"
_OPS_LLM = frozenset({"chat", "text_completion", "generate_content"})
_OPS_AGENT = frozenset({"invoke_agent", "create_agent"})
_OPS_TOOL = frozenset({"execute_tool"})

_AGENT_NAME = "gen_ai.agent.name"
_TOOL_NAME = "gen_ai.tool.name"
_REQUEST_MODEL = "gen_ai.request.model"
_RESPONSE_MODEL = "gen_ai.response.model"
_PROVIDER = "gen_ai.provider.name"
_INPUT_TOKENS = "gen_ai.usage.input_tokens"
_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
_CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
_CACHE_WRITE_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
_INPUT_MESSAGES = "gen_ai.input.messages"
_OUTPUT_MESSAGES = "gen_ai.output.messages"
_TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
_TOOL_RESULT = "gen_ai.tool.call.result"

# --- Port point: Pydantic-AI-specific keys, NOT semantic convention
_PAI_COST = "operation.cost"
_PAI_FINAL_RESULT = "final_result"
_PAI_ALL_MESSAGES = "pydantic_ai.all_messages"
# Emitted on the agent-run span as a roll-up of its children. Deliberately
# ignored: summing it alongside the per-request `gen_ai.usage.*` would report
# exactly twice the real token count. Token totals come from LLM spans only.
_PAI_AGGREGATED_USAGE_PREFIX = "gen_ai.aggregated_usage."

# --- Our own instrumentation (app/observability/setup.py). Not framework
# --- coupled: these survive an agent-framework swap untouched.
_DB_SYSTEM = "db.system"
_HTTP_ROUTE = "http.route"
_MEMBER_ID = "kg_coach.member_id"

OPERATION_NAME = "kg_coach.operation"
"""Marks a span as one step of the deterministic tool layer."""

OPERATION_SUMMARY = "kg_coach.summary"
"""One-line outcome the Traces timeline shows instead of the span name."""

# Attributes that are either stored in a dedicated column or are bulky
# content. Stripped from the `attributes` blob so message history is never
# written twice, once truncated and once in full.
_STRIPPED_FROM_ATTRIBUTES = frozenset(
    {
        _OPERATION,
        _AGENT_NAME,
        _TOOL_NAME,
        _REQUEST_MODEL,
        _RESPONSE_MODEL,
        _PROVIDER,
        _INPUT_TOKENS,
        _OUTPUT_TOKENS,
        _CACHE_READ_TOKENS,
        _CACHE_WRITE_TOKENS,
        _INPUT_MESSAGES,
        _OUTPUT_MESSAGES,
        _TOOL_ARGUMENTS,
        _TOOL_RESULT,
        _PAI_COST,
        _PAI_FINAL_RESULT,
        _PAI_ALL_MESSAGES,
        _HTTP_ROUTE,
        _MEMBER_ID,
        # Large and useless once the tool call itself is recorded.
        "gen_ai.system_instructions",
        "gen_ai.tool.definitions",
        "model_request_parameters",
        "logfire.json_schema",
        "logfire.msg",
    }
)

_MICRO_USD = 1_000_000


class SpanCategory(StrEnum):
    """What kind of work a span represents, in the app's own vocabulary."""

    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    DB = "db"
    HTTP = "http"
    OTHER = "other"


class SpanStatus(StrEnum):
    """Outcome of a span, flattened from OpenTelemetry's status code."""

    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


class SpanRecord(BaseModel):
    """One span, in storage shape. Mirrors the `spans` table one-to-one."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    category: SpanCategory
    started_at: datetime
    ended_at: datetime
    duration_ms: float
    status: SpanStatus
    error_message: str | None = None

    agent_name: str | None = None
    model: str | None = None
    provider: str | None = None
    tool_name: str | None = None
    route: str | None = None
    member_id: str | None = None

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micro_usd: int = 0

    input_preview: str | None = None
    output_preview: str | None = None
    input_truncated: bool = False
    output_truncated: bool = False

    attributes: dict[str, Any] = Field(default_factory=dict)


def classify(attributes: Mapping[str, Any], span_name: str) -> SpanCategory:
    """Decide what kind of work a span represents.

    Checks semantic-convention attributes before anything framework-specific,
    and never inspects the span name for a category — see the module header.

    Args:
        attributes: The span's OpenTelemetry attributes.
        span_name: The span name, used only as a last-resort tiebreak so this
            signature still works for a framework that sets no attributes.

    Returns:
        The matching SpanCategory, or ``OTHER`` when nothing matches.
    """
    if _DB_SYSTEM in attributes:
        return SpanCategory.DB
    # The resolver and safety traversal are the deterministic tool layer, the
    # same role the agent framework's own tool calls play, so they share a
    # category rather than inventing one the UI would have to learn.
    if OPERATION_NAME in attributes:
        return SpanCategory.TOOL

    operation = attributes.get(_OPERATION)
    if operation in _OPS_LLM:
        return SpanCategory.LLM
    if operation in _OPS_AGENT:
        return SpanCategory.AGENT
    if operation in _OPS_TOOL:
        return SpanCategory.TOOL

    # Forward compatibility: some instrumentations (OpenInference, several
    # LangChain integrations) set the GenAI attributes without ever setting
    # gen_ai.operation.name. Fall back to the attribute that must be present
    # for the span to mean anything.
    if _TOOL_NAME in attributes:
        return SpanCategory.TOOL
    if _REQUEST_MODEL in attributes or _RESPONSE_MODEL in attributes:
        return SpanCategory.LLM
    if _AGENT_NAME in attributes:
        return SpanCategory.AGENT
    if _HTTP_ROUTE in attributes:
        return SpanCategory.HTTP

    return SpanCategory.OTHER


def map_span(
    span: ReadableSpan, *, capture_content: bool, content_max_chars: int
) -> SpanRecord:
    """Convert a raw OpenTelemetry span into the app's storage shape.

    Args:
        span: A finished span handed over by the batch span processor.
        capture_content: Whether to keep prompt/completion text at all.
        content_max_chars: Character cap applied to each captured preview.

    Returns:
        The SpanRecord to persist.
    """
    attributes: Mapping[str, Any] = span.attributes or {}
    category = classify(attributes, span.name)

    started_at = _to_datetime(span.start_time)
    ended_at = _to_datetime(span.end_time)
    status, error_message = _status_of(span)

    record = SpanRecord(
        trace_id=f"{span.context.trace_id:032x}",
        span_id=f"{span.context.span_id:016x}",
        parent_span_id=(
            f"{span.parent.span_id:016x}" if span.parent is not None else None
        ),
        name=span.name,
        category=category,
        started_at=started_at,
        ended_at=ended_at,
        # Nanosecond timestamps, so this is exact rather than a clock re-read.
        duration_ms=(span.end_time - span.start_time) / 1_000_000,
        status=status,
        error_message=error_message,
        agent_name=_str_or_none(attributes.get(_AGENT_NAME)),
        model=_str_or_none(
            attributes.get(_RESPONSE_MODEL) or attributes.get(_REQUEST_MODEL)
        ),
        provider=_str_or_none(attributes.get(_PROVIDER)),
        tool_name=_str_or_none(
            attributes.get(_TOOL_NAME) or attributes.get(OPERATION_NAME)
        ),
        route=_str_or_none(attributes.get(_HTTP_ROUTE)),
        member_id=_str_or_none(attributes.get(_MEMBER_ID)),
        attributes=_display_attributes(attributes),
    )

    # Token and cost figures come from LLM spans only. See the note on
    # _PAI_AGGREGATED_USAGE_PREFIX: reading them off the agent-run span too
    # would double every total the Traces page shows.
    if category is SpanCategory.LLM:
        record.input_tokens = _int_or_zero(attributes.get(_INPUT_TOKENS))
        record.output_tokens = _int_or_zero(attributes.get(_OUTPUT_TOKENS))
        record.cache_read_tokens = _int_or_zero(attributes.get(_CACHE_READ_TOKENS))
        record.cache_write_tokens = _int_or_zero(attributes.get(_CACHE_WRITE_TOKENS))
        record.cost_micro_usd = _cost_micro_usd(attributes)

    if capture_content:
        raw_input, raw_output = _content_of(category, attributes)
        record.input_preview, record.input_truncated = _truncate(
            raw_input, content_max_chars
        )
        record.output_preview, record.output_truncated = _truncate(
            raw_output, content_max_chars
        )

    return record


def _content_of(
    category: SpanCategory, attributes: Mapping[str, Any]
) -> tuple[Any, Any]:
    """Pick the input/output payload worth previewing for a span category.

    Args:
        category: The span's classified category.
        attributes: The span's OpenTelemetry attributes.

    Returns:
        A ``(input, output)`` pair of raw attribute values, either of which
        may be None when the span carries no such content.
    """
    if category is SpanCategory.LLM:
        return attributes.get(_INPUT_MESSAGES), attributes.get(_OUTPUT_MESSAGES)
    if category is SpanCategory.TOOL:
        return attributes.get(_TOOL_ARGUMENTS), attributes.get(_TOOL_RESULT)
    if category is SpanCategory.AGENT:
        # The agent-run span's input is the prompt already shown on its first
        # child LLM span, so only the final output is worth storing again.
        return None, attributes.get(_PAI_FINAL_RESULT)
    return None, None


def _cost_micro_usd(attributes: Mapping[str, Any]) -> int:
    """Read the request's cost, in integer micro-USD.

    Pydantic AI computes this with `genai-prices` (an offline price snapshot,
    evaluated at the request timestamp) and sets it on the model-request span,
    so there is no price table to maintain here. Integer micro-USD rather than
    a float or NUMERIC keeps sums exact and identical on Postgres and SQLite.

    Args:
        attributes: The span's OpenTelemetry attributes.

    Returns:
        The cost in micro-USD, or 0 when the framework did not price the call.
    """
    cost = attributes.get(_PAI_COST)
    if not isinstance(cost, (int, float)):
        return 0
    return round(float(cost) * _MICRO_USD)


def _display_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the attributes worth showing, minus content and promoted columns.

    Args:
        attributes: The span's OpenTelemetry attributes.

    Returns:
        A JSON-serialisable dict for the span detail panel.
    """
    return {
        key: value
        for key, value in attributes.items()
        if key not in _STRIPPED_FROM_ATTRIBUTES
        and not key.startswith(_PAI_AGGREGATED_USAGE_PREFIX)
    }


def _truncate(value: Any, max_chars: int) -> tuple[str | None, bool]:
    """Render a value as text and cap its length.

    Args:
        value: Raw attribute value; may be a JSON string or any scalar.
        max_chars: Maximum characters to keep.

    Returns:
        A ``(text, was_truncated)`` pair; text is None when there is nothing
        to show.
    """
    if value is None:
        return None, False
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _status_of(span: ReadableSpan) -> tuple[SpanStatus, str | None]:
    """Flatten a span's OpenTelemetry status into a status and a message.

    Args:
        span: The finished span.

    Returns:
        A ``(status, error_message)`` pair; the message is set only on error.
    """
    code = span.status.status_code
    if code is StatusCode.ERROR:
        return SpanStatus.ERROR, span.status.description or _exception_message(span)
    if code is StatusCode.OK:
        return SpanStatus.OK, None
    return SpanStatus.UNSET, None


def _exception_message(span: ReadableSpan) -> str | None:
    """Recover an error message from a span's recorded exception events.

    A span whose status was set without a description still carries the
    exception detail as an event, which is what a reader actually needs.

    Args:
        span: The finished span.

    Returns:
        The first recorded exception message, or None.
    """
    for event in span.events:
        if event.name == "exception":
            message = (event.attributes or {}).get("exception.message")
            if message:
                return str(message)
    return None


def _to_datetime(epoch_nanoseconds: int) -> datetime:
    """Convert an OpenTelemetry nanosecond timestamp to an aware datetime."""
    return datetime.fromtimestamp(epoch_nanoseconds / 1_000_000_000, tz=UTC)


def _str_or_none(value: Any) -> str | None:
    """Coerce an attribute value to a string, mapping absent/empty to None."""
    return str(value) if value not in (None, "") else None


def _int_or_zero(value: Any) -> int:
    """Coerce an attribute value to an int, mapping anything unusable to 0."""
    return int(value) if isinstance(value, (int, float)) else 0
