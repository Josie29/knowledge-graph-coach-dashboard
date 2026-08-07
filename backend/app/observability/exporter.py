from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.observability.ingest import map_span
from app.observability.store import TraceStore

logger = logging.getLogger(__name__)

# One complaint per interval, however many batches fail. A trace store that is
# down would otherwise print a traceback every few seconds and bury the logs
# that matter.
_ERROR_LOG_INTERVAL_SECONDS = 60.0


class SqlSpanExporter(SpanExporter):
    """Writes finished spans into the local trace store.

    Runs synchronously on the batch span processor's worker thread, which is
    why it holds no event loop and no threads of its own: the processor
    already provides batching, a bounded queue, and a drain on shutdown.

    Nothing in here is allowed to raise. Traces are diagnostics, not the
    product; a trace-store outage must never surface in a user's request.
    """

    def __init__(
        self,
        store: TraceStore,
        *,
        capture_content: bool,
        content_max_chars: int,
        retention: timedelta,
        sweep_interval_seconds: float,
    ) -> None:
        """Configure the exporter.

        Args:
            store: The trace store to write to.
            capture_content: Whether to keep prompt and completion text.
            content_max_chars: Cap applied to each captured preview.
            retention: How long spans are kept before the sweep removes them.
            sweep_interval_seconds: Minimum gap between retention sweeps.
        """
        self._store = store
        self._capture_content = capture_content
        self._content_max_chars = content_max_chars
        self._retention = retention
        self._sweep_interval = sweep_interval_seconds
        # Zero rather than "now", so the first batch after a restart sweeps
        # immediately instead of leaving expired spans for another interval.
        self._last_sweep = 0.0
        self._last_error_log = 0.0
        self._dropped_batches = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Persist a batch of finished spans.

        Args:
            spans: The batch handed over by the span processor.

        Returns:
            SUCCESS when the batch was committed, FAILURE otherwise. The
            processor has already removed these spans from its queue, so a
            FAILURE means the batch is lost, not retried.
        """
        if not spans:
            return SpanExportResult.SUCCESS
        if not self._store.ensure_schema():
            return self._record_failure(None)

        try:
            records = [
                map_span(
                    span,
                    capture_content=self._capture_content,
                    content_max_chars=self._content_max_chars,
                )
                for span in spans
            ]
            self._store.write_spans(records)
        except sa.exc.SQLAlchemyError as exc:
            return self._record_failure(exc)
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            # A mapping bug must not take down the export thread either; the
            # batch is dropped and the traceback is logged once per interval.
            return self._record_failure(exc)

        self._sweep_if_due()
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Report that nothing is buffered.

        Correct by construction: `export` only returns once its transaction
        has committed, so there is never pending work to flush here.

        Args:
            timeout_millis: Unused; part of the SpanExporter interface.

        Returns:
            True, always.
        """
        return True

    def shutdown(self) -> None:
        """Close the store's connection pool."""
        self._store.dispose()

    def _sweep_if_due(self) -> None:
        """Delete expired spans, at most once per configured interval.

        Piggybacks on export rather than a scheduler, so it needs no cron, no
        background task, and no extra container. Self-limiting: no spans
        produced means no sweep runs.
        """
        now = time.monotonic()
        if now - self._last_sweep < self._sweep_interval:
            return
        self._last_sweep = now
        try:
            deleted = self._store.prune(datetime.now(UTC) - self._retention)
        except sa.exc.SQLAlchemyError as exc:
            # A failed sweep must never fail the export that triggered it.
            logger.warning("Trace retention sweep failed: %s", exc)
            return
        if deleted:
            logger.info("Trace retention sweep removed %d spans", deleted)

    def _record_failure(self, exc: Exception | None) -> SpanExportResult:
        """Count a dropped batch and log at most once per interval.

        Args:
            exc: The failure, if there was an exception to report.

        Returns:
            SpanExportResult.FAILURE, for the caller to return.
        """
        self._dropped_batches += 1
        now = time.monotonic()
        if now - self._last_error_log >= _ERROR_LOG_INTERVAL_SECONDS:
            self._last_error_log = now
            logger.warning(
                "Trace export failed (%d batches dropped so far): %s",
                self._dropped_batches,
                exc if exc is not None else "trace store schema unavailable",
            )
        return SpanExportResult.FAILURE
