from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine, Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.observability.ingest import SpanCategory, SpanRecord, SpanStatus

logger = logging.getLogger(__name__)

_metadata = sa.MetaData()

# One table, no rollup table. Trace summaries are a GROUP BY computed at read
# time, which is correct by construction: spans arrive out of order and a
# parent always ends after its children, so any materialised rollup would need
# incremental-merge logic to stay right. At this scale (thousands of rows under
# a 7-day retention) the aggregate is free, and materialising later is a purely
# additive change.
#
# Column types are chosen to mean the same thing on Postgres and SQLite:
# cost is integer micro-USD rather than NUMERIC (which round-trips as Decimal
# on one and float on the other), and `attributes` is generic JSON that is
# never filtered on in SQL.
spans_table = sa.Table(
    "spans",
    _metadata,
    sa.Column("span_id", sa.String(16), primary_key=True),
    sa.Column("trace_id", sa.String(32), nullable=False),
    sa.Column("parent_span_id", sa.String(16), nullable=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("category", sa.String(10), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("duration_ms", sa.Float, nullable=False),
    sa.Column("status", sa.String(10), nullable=False),
    sa.Column("error_message", sa.Text, nullable=True),
    sa.Column("agent_name", sa.String(100), nullable=True),
    sa.Column("model", sa.String(100), nullable=True),
    sa.Column("provider", sa.String(50), nullable=True),
    sa.Column("tool_name", sa.String(100), nullable=True),
    sa.Column("route", sa.String(200), nullable=True),
    sa.Column("member_id", sa.String(64), nullable=True),
    sa.Column("input_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("output_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("cache_read_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("cache_write_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("cost_micro_usd", sa.BigInteger, nullable=False, default=0),
    sa.Column("input_preview", sa.Text, nullable=True),
    sa.Column("output_preview", sa.Text, nullable=True),
    sa.Column("input_truncated", sa.Boolean, nullable=False, default=False),
    sa.Column("output_truncated", sa.Boolean, nullable=False, default=False),
    sa.Column("attributes", sa.JSON, nullable=False, default=dict),
    sa.Index("ix_spans_trace_started", "trace_id", "started_at"),
    sa.Index("ix_spans_started", "started_at"),
)


class TraceFilter(StrEnum):
    """Which traces the list should return."""

    ALL = "all"
    AI = "ai"
    ERRORS = "errors"


class TraceSummary(BaseModel):
    """One request's worth of spans, rolled up for the traces list."""

    trace_id: str
    name: str
    route: str | None
    # Every agent that ran, in the order they started, and the model they used.
    # These come from the trace's spans rather than its root span: the root is
    # the HTTP request, which has no agent, so reading them off it would make
    # the list say "POST /api/workout" where it should say what actually ran.
    # Required rather than defaulted, so the generated frontend type is a plain
    # array instead of an optional one.
    agent_names: list[str]
    model: str | None
    member_id: str | None
    started_at: datetime
    duration_ms: float
    status: SpanStatus
    span_count: int
    llm_count: int
    tool_count: int
    db_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int


class TraceSpan(BaseModel):
    """One span as the trace detail view shows it."""

    span_id: str
    parent_span_id: str | None
    name: str
    category: SpanCategory
    started_at: datetime
    duration_ms: float
    status: SpanStatus
    error_message: str | None
    model: str | None
    tool_name: str | None
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    input_preview: str | None
    output_preview: str | None
    input_truncated: bool
    output_truncated: bool
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceDetail(BaseModel):
    """A trace summary plus every span in it, ordered by start time."""

    summary: TraceSummary
    spans: list[TraceSpan]


class TraceStats(BaseModel):
    """Aggregate figures over a recent window, for the traces page header."""

    window_hours: int
    trace_count: int
    error_trace_count: int
    llm_call_count: int
    graph_query_count: int
    total_tokens: int
    total_cost_micro_usd: int
    p50_duration_ms: float
    p95_duration_ms: float


class TraceStore:
    """Reads and writes spans in a local SQL database.

    Dialect-neutral on purpose: the same code runs against the Postgres
    service in docker-compose and against a SQLite file for local runs and
    tests, selected by URL alone.
    """

    def __init__(self, database_url: str) -> None:
        """Open a connection pool for the trace store.

        Args:
            database_url: A SQLAlchemy URL, e.g. ``postgresql+psycopg://...``
                or ``sqlite+pysqlite:///traces.db``.
        """
        self._engine: Engine = sa.create_engine(
            database_url, **_engine_options(database_url)
        )
        self._schema_ready = False

    @property
    def engine(self) -> Engine:
        """The underlying engine, exposed for tests and shutdown."""
        return self._engine

    def create_schema(self) -> None:
        """Create the spans table if it does not exist.

        Raises:
            SQLAlchemyError: If the database is unreachable. Callers decide
                whether that is fatal; for the API it is not.
        """
        _metadata.create_all(self._engine)
        self._schema_ready = True

    def ensure_schema(self) -> bool:
        """Try to create the schema, reporting failure instead of raising.

        Lets the exporter recover on its own when the database was down at
        startup, without a retry loop on the hot path.

        Returns:
            True when the schema is ready.
        """
        if self._schema_ready:
            return True
        try:
            self.create_schema()
        except sa.exc.SQLAlchemyError as exc:
            logger.warning("Trace store schema not ready: %s", exc)
            return False
        return True

    def write_spans(self, records: Sequence[SpanRecord]) -> int:
        """Insert spans, skipping any that are already stored.

        Args:
            records: Finished spans to persist.

        Returns:
            The number of rows actually inserted.

        Raises:
            SQLAlchemyError: On connection or statement failure. The exporter
                is responsible for swallowing this.
        """
        if not records:
            return 0
        # Python mode, not JSON: the DateTime columns need real datetime
        # objects, and StrEnum members are already valid String values.
        rows = [record.model_dump() for record in records]
        try:
            with self._engine.begin() as conn:
                conn.execute(sa.insert(spans_table), rows)
            return len(rows)
        except IntegrityError:
            # A batch replayed after a failed export repeats span ids. Fall
            # back to row-at-a-time so the new spans still land; without this
            # one duplicate would discard the whole batch.
            return self._write_spans_individually(rows)

    def _write_spans_individually(self, rows: list[dict[str, Any]]) -> int:
        """Insert rows one by one, ignoring duplicate primary keys.

        Args:
            rows: Span rows already serialised for the database.

        Returns:
            The number of rows inserted.
        """
        inserted = 0
        for row in rows:
            try:
                with self._engine.begin() as conn:
                    conn.execute(sa.insert(spans_table), [row])
                inserted += 1
            except IntegrityError:
                continue
        return inserted

    def prune(self, cutoff: datetime) -> int:
        """Delete spans that started before a cutoff.

        Args:
            cutoff: Everything older than this is removed.

        Returns:
            The number of rows deleted.

        Raises:
            SQLAlchemyError: On connection or statement failure.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.delete(spans_table).where(spans_table.c.started_at < cutoff)
            )
        return result.rowcount or 0

    def count_traces(self) -> int:
        """Count distinct traces currently stored, for the health endpoint."""
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count(sa.distinct(spans_table.c.trace_id)))
                ).scalar_one()
            )

    def list_traces(
        self, *, limit: int = 50, trace_filter: TraceFilter = TraceFilter.ALL
    ) -> list[TraceSummary]:
        """Roll spans up into the most recent traces.

        Args:
            limit: Maximum traces to return, newest first.
            trace_filter: Which traces to keep — everything, only those with an
                LLM call, or only those containing a failed span.

        Returns:
            Trace summaries ordered newest first.
        """
        with self._engine.connect() as conn:
            # Filtering happens in SQL, before the limit. Post-filtering a
            # limited page would answer "errors among the newest 50" when the
            # question was "the newest 50 errors".
            aggregates = conn.execute(
                self._summary_query(trace_filter).limit(limit)
            ).all()
            if not aggregates:
                return []
            trace_ids = [row.trace_id for row in aggregates]
            roots = self._root_spans(conn, trace_ids)
            labels = self._agent_labels(conn, trace_ids)

        return [
            self._to_summary(row, roots.get(row.trace_id), *labels[row.trace_id])
            for row in aggregates
        ]

    def get_trace(self, trace_id: str) -> TraceDetail | None:
        """Fetch one trace with all of its spans.

        Args:
            trace_id: The 32-character hex trace id.

        Returns:
            The trace detail, or None when no such trace is stored.
        """
        with self._engine.connect() as conn:
            aggregate = conn.execute(
                self._summary_query().where(spans_table.c.trace_id == trace_id)
            ).first()
            if aggregate is None:
                return None
            roots = self._root_spans(conn, [trace_id])
            labels = self._agent_labels(conn, [trace_id])
            span_rows = conn.execute(
                sa.select(spans_table)
                .where(spans_table.c.trace_id == trace_id)
                .order_by(spans_table.c.started_at)
            ).all()

        return TraceDetail(
            summary=self._to_summary(
                aggregate, roots.get(trace_id), *labels[trace_id]
            ),
            spans=[
                TraceSpan(
                    span_id=row.span_id,
                    parent_span_id=row.parent_span_id,
                    name=row.name,
                    category=SpanCategory(row.category),
                    started_at=_as_utc(row.started_at),
                    duration_ms=row.duration_ms,
                    status=SpanStatus(row.status),
                    error_message=row.error_message,
                    model=row.model,
                    tool_name=row.tool_name,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    cost_micro_usd=row.cost_micro_usd,
                    input_preview=row.input_preview,
                    output_preview=row.output_preview,
                    input_truncated=row.input_truncated,
                    output_truncated=row.output_truncated,
                    attributes=row.attributes or {},
                )
                for row in span_rows
            ],
        )

    def stats(
        self,
        *,
        window_hours: int = 24,
        trace_filter: TraceFilter = TraceFilter.ALL,
    ) -> TraceStats:
        """Summarise recent activity for the traces page header.

        Args:
            window_hours: How far back to aggregate.
            trace_filter: Restricts the population the figures describe, so the
                header agrees with the list shown beneath it.

        Returns:
            Counts, token and cost totals, and latency percentiles. Empty
            windows return zeroes rather than None.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._summary_query(trace_filter).where(
                    spans_table.c.started_at >= cutoff
                )
            ).all()

        durations = sorted(
            (_as_utc(row.ended_at) - _as_utc(row.started_at)).total_seconds() * 1000
            for row in rows
        )
        return TraceStats(
            window_hours=window_hours,
            trace_count=len(rows),
            error_trace_count=sum(1 for row in rows if row.error_count),
            llm_call_count=sum(row.llm_count for row in rows),
            graph_query_count=sum(row.db_count for row in rows),
            total_tokens=sum(row.input_tokens + row.output_tokens for row in rows),
            total_cost_micro_usd=sum(row.cost_micro_usd for row in rows),
            p50_duration_ms=_percentile(durations, 0.50),
            p95_duration_ms=_percentile(durations, 0.95),
        )

    def dispose(self) -> None:
        """Close every pooled connection."""
        self._engine.dispose()

    @staticmethod
    def _summary_query(
        trace_filter: TraceFilter = TraceFilter.ALL,
    ) -> sa.Select[Any]:
        """Build the per-trace aggregate used by list, detail, and stats.

        Args:
            trace_filter: Restricts the result with a HAVING clause, so the
                filter applies before any LIMIT the caller adds.

        Returns:
            A select grouped by trace id, newest trace first. It returns the
            first and last timestamps rather than a duration: subtracting
            timestamps in SQL needs `EXTRACT(EPOCH ...)` on Postgres and
            `julianday()` on SQLite, so the arithmetic happens in Python to
            keep one query working on both.
        """
        started_at = sa.func.min(spans_table.c.started_at).label("started_at")
        llm_count = _count_where(spans_table.c.category == SpanCategory.LLM)
        error_count = _count_where(spans_table.c.status == SpanStatus.ERROR)
        query = (
            sa.select(
                spans_table.c.trace_id,
                started_at,
                sa.func.max(spans_table.c.ended_at).label("ended_at"),
                sa.func.count().label("span_count"),
                llm_count.label("llm_count"),
                _count_where(spans_table.c.category == SpanCategory.TOOL).label(
                    "tool_count"
                ),
                _count_where(spans_table.c.category == SpanCategory.DB).label(
                    "db_count"
                ),
                error_count.label("error_count"),
                sa.func.sum(spans_table.c.input_tokens).label("input_tokens"),
                sa.func.sum(spans_table.c.output_tokens).label("output_tokens"),
                sa.func.sum(spans_table.c.cost_micro_usd).label("cost_micro_usd"),
            )
            .group_by(spans_table.c.trace_id)
            .order_by(started_at.desc())
        )
        if trace_filter is TraceFilter.AI:
            query = query.having(llm_count > 0)
        elif trace_filter is TraceFilter.ERRORS:
            query = query.having(error_count > 0)
        return query

    @staticmethod
    def _root_spans(conn: sa.Connection, trace_ids: Sequence[str]) -> dict[str, Row[Any]]:
        """Fetch the identifying (parentless) span of each given trace.

        Args:
            conn: An open connection.
            trace_ids: Traces to look up.

        Returns:
            Trace id to its root span row. A trace whose root span was dropped
            or is still in flight is simply absent.
        """
        rows = conn.execute(
            sa.select(spans_table)
            .where(spans_table.c.trace_id.in_(trace_ids))
            .where(spans_table.c.parent_span_id.is_(None))
        ).all()
        return {row.trace_id: row for row in rows}

    @staticmethod
    def _agent_labels(
        conn: sa.Connection, trace_ids: Sequence[str]
    ) -> dict[str, tuple[list[str], str | None]]:
        """Collect the agents and model that ran inside each given trace.

        A separate query rather than a grouped aggregate because concatenating
        strings per group is `string_agg` on Postgres and `group_concat` on
        SQLite — the one place where the two dialects would have forced a
        split. Ordering by start time keeps the agents in execution order, so a
        workout reads "constraint-extractor -> workout-planner".

        Args:
            conn: An open connection.
            trace_ids: Traces to look up.

        Returns:
            Trace id to its ``(agent names, model)`` pair. Traces with neither
            map to an empty list and None.
        """
        rows = conn.execute(
            sa.select(
                spans_table.c.trace_id,
                spans_table.c.agent_name,
                spans_table.c.model,
            )
            .where(spans_table.c.trace_id.in_(trace_ids))
            .where(
                sa.or_(
                    spans_table.c.agent_name.is_not(None),
                    spans_table.c.model.is_not(None),
                )
            )
            .order_by(spans_table.c.started_at)
        ).all()

        agents: dict[str, list[str]] = {}
        models: dict[str, str] = {}
        for row in rows:
            if row.agent_name:
                names = agents.setdefault(row.trace_id, [])
                if row.agent_name not in names:
                    names.append(row.agent_name)
            if row.model and row.trace_id not in models:
                models[row.trace_id] = row.model
        return {
            trace_id: (agents.get(trace_id, []), models.get(trace_id))
            for trace_id in trace_ids
        }

    @staticmethod
    def _to_summary(
        aggregate: Row[Any],
        root: Row[Any] | None,
        agent_names: list[str],
        model: str | None,
    ) -> TraceSummary:
        """Combine a trace's aggregate row with its root span and agent labels.

        Args:
            aggregate: One row from `_summary_query`.
            root: The trace's parentless span, when it has been stored.
            agent_names: Agents that ran, in execution order.
            model: The model used, if any.

        Returns:
            The trace summary. Falls back to neutral values when the root span
            is missing, so an in-flight or crashed run still renders instead of
            disappearing from the list.
        """
        started_at = _as_utc(aggregate.started_at)
        ended_at = _as_utc(aggregate.ended_at)
        return TraceSummary(
            trace_id=aggregate.trace_id,
            name=root.name if root is not None else "(incomplete trace)",
            route=root.route if root is not None else None,
            agent_names=agent_names,
            model=model,
            member_id=root.member_id if root is not None else None,
            started_at=started_at,
            # Wall-clock across every span, not the root span's own duration,
            # so a trace whose root was lost still reports a sane elapsed time.
            duration_ms=(ended_at - started_at).total_seconds() * 1000,
            status=(
                SpanStatus.ERROR if aggregate.error_count else SpanStatus.OK
            ),
            span_count=aggregate.span_count,
            llm_count=aggregate.llm_count,
            tool_count=aggregate.tool_count,
            db_count=aggregate.db_count,
            error_count=aggregate.error_count,
            input_tokens=aggregate.input_tokens or 0,
            output_tokens=aggregate.output_tokens or 0,
            cost_micro_usd=aggregate.cost_micro_usd or 0,
        )


def _engine_options(database_url: str) -> dict[str, Any]:
    """Pick engine options appropriate to the database dialect.

    Args:
        database_url: The SQLAlchemy URL being opened.

    Returns:
        Keyword arguments for `create_engine`.
    """
    # pool_pre_ping lets the API survive a database container restart without
    # handing out a dead connection on the next export.
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        # Spans are written from the span processor's worker thread, not the
        # thread that opened the connection.
        options["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url or database_url.endswith("//"):
            # Without StaticPool an in-memory database is per-connection, so
            # the writer and the reader would see different databases.
            options["poolclass"] = StaticPool
    return options


def _count_where(condition: sa.ColumnElement[bool]) -> sa.ColumnElement[int]:
    """Count rows matching a condition inside a grouped query.

    `count(CASE WHEN ...)` rather than a filtered aggregate: portable to both
    supported dialects.

    Args:
        condition: The predicate to count.

    Returns:
        An aggregate expression usable in a select list.
    """
    return sa.func.sum(sa.case((condition, 1), else_=0))


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of an already-sorted sequence.

    Args:
        sorted_values: Values in ascending order.
        fraction: The percentile as a fraction, e.g. 0.95.

    Returns:
        The percentile value, or 0.0 for an empty sequence.
    """
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(round(fraction * len(sorted_values))) - 1)
    return sorted_values[max(index, 0)]


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp.

    Postgres returns timezone-aware datetimes and SQLite returns naive ones;
    normalising here keeps the API contract identical on both.

    Args:
        value: A timestamp read from the database.

    Returns:
        The same instant, timezone-aware.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
