from __future__ import annotations

import logging

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.observability.store import (
    TraceDetail,
    TraceFilter,
    TraceStats,
    TraceStore,
    TraceSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/traces", tags=["observability"])


def _store_of(request: Request) -> TraceStore:
    """Get the trace store, or fail the request cleanly when tracing is off.

    Args:
        request: The incoming request.

    Returns:
        The configured trace store.

    Raises:
        HTTPException: 503 when tracing is disabled for this deployment.
    """
    store: TraceStore | None = getattr(request.app.state, "trace_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="tracing is disabled; set OBS_ENABLED=true to record traces",
        )
    return store


# Reads are synchronous SQLAlchemy, so they run in a worker thread rather than
# blocking the event loop. `/stats` is declared before `/{trace_id}` because
# FastAPI matches routes in order and would otherwise read "stats" as an id.


@router.get("", response_model=list[TraceSummary])
async def list_traces(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    show: TraceFilter = Query(
        TraceFilter.ALL,
        description=(
            "all: every traced request. ai: only traces containing an LLM "
            "call. errors: only traces containing a failed span."
        ),
    ),
) -> list[TraceSummary]:
    """List recent traces, newest first.

    Args:
        request: The incoming request.
        limit: Maximum traces to return.
        show: Which traces to keep. Filtering happens before the limit.

    Returns:
        Trace summaries with span counts, token usage, and cost.

    Raises:
        HTTPException: 503 when tracing is disabled or the store is unreachable.
    """
    store = _store_of(request)
    try:
        return await run_in_threadpool(
            store.list_traces, limit=limit, trace_filter=show
        )
    except sa.exc.SQLAlchemyError as exc:
        raise _unavailable(exc) from exc


@router.get("/stats", response_model=TraceStats)
async def trace_stats(
    request: Request, window_hours: int = Query(24, ge=1, le=168)
) -> TraceStats:
    """Summarise recent tracing activity.

    Args:
        request: The incoming request.
        window_hours: How far back to aggregate, up to one week.

    Returns:
        Counts, token and cost totals, and latency percentiles.

    Raises:
        HTTPException: 503 when tracing is disabled or the store is unreachable.
    """
    store = _store_of(request)
    try:
        return await run_in_threadpool(store.stats, window_hours=window_hours)
    except sa.exc.SQLAlchemyError as exc:
        raise _unavailable(exc) from exc


@router.get("/{trace_id}", response_model=TraceDetail)
async def get_trace(request: Request, trace_id: str) -> TraceDetail:
    """Fetch one trace and every span inside it.

    Args:
        request: The incoming request.
        trace_id: The 32-character hex trace id.

    Returns:
        The trace summary plus its spans, ordered by start time.

    Raises:
        HTTPException: 404 when no such trace is stored, 503 when tracing is
            disabled or the store is unreachable.
    """
    store = _store_of(request)
    try:
        detail = await run_in_threadpool(store.get_trace, trace_id)
    except sa.exc.SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    return detail


def _unavailable(exc: Exception) -> HTTPException:
    """Turn a store failure into a 503, logging the cause.

    Args:
        exc: The underlying database error.

    Returns:
        The HTTPException to raise.
    """
    logger.error("Trace store read failed: %s", exc)
    return HTTPException(status_code=503, detail="trace store unavailable")
