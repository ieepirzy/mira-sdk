"""Run-scoped OTLP telemetry export for Mira agent processes.

One `MiraTelemetry` instance is a TracerProvider scoped to a single run: it
sets Mira's identifying attributes once as OTel resource attributes and
exports spans over OTLP/HTTP, non-blocking, on a background thread.

Non-blocking is structural, not a convention to remember: this module only
ever wires a `BatchSpanProcessor` (queue + background export thread), never
a `SimpleSpanProcessor` (exports inline on span end). A telemetry backend
being unreachable must never fail or stall the operation that emitted the
span — that guarantee has to live here, not be re-implemented by every
caller.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

logger = logging.getLogger("mira_sdk.telemetry")

_DEFAULT_MAX_QUEUE_SIZE = 2048
_DEFAULT_MAX_EXPORT_BATCH_SIZE = 512
_DEFAULT_SCHEDULE_DELAY_MILLIS = 5000
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 30000


@dataclass
class ExportStats:
    """Export-level bookkeeping, readable at any time.

    This tracks batches that reached the exporter and failed — the
    actionable half of "did my telemetry get out." It deliberately does not
    track queue-overflow drops (a producer outpacing the background export
    thread): OpenTelemetry's own `BatchSpanProcessor` already logs those
    through the standard `opentelemetry.sdk.trace.export` logger, and
    duplicating that bookkeeping here would mean relying on undocumented
    SDK internals for a number this module can't verify independently.
    """

    attempted_batches: int = 0
    failed_batches: int = 0
    failed_spans: int = 0


class MiraTelemetry:
    """Configures one OTel TracerProvider scoped to a single Mira run.

    Resource attributes (run/agent/routine identity, service name/version,
    deployment environment) are set once here and are on every span this
    instance emits — attaching them per-span instead is the mistake that
    breaks cross-span filtering and aggregation once the telemetry backend
    is in front of you and you can't retrofit them.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        run_id: str,
        agent_id: str | None = None,
        routine_id: str | None = None,
        routine_revision_id: str | None = None,
        service_name: str = "mira-agent",
        service_version: str | None = None,
        deployment_environment: str | None = None,
        token: str | None = None,
        extra_resource_attributes: Mapping[str, str] | None = None,
        max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE,
        max_export_batch_size: int = _DEFAULT_MAX_EXPORT_BATCH_SIZE,
        schedule_delay_millis: int = _DEFAULT_SCHEDULE_DELAY_MILLIS,
        export_timeout_millis: int = _DEFAULT_EXPORT_TIMEOUT_MILLIS,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        """
        `endpoint` is the OTLP/HTTP traces endpoint (e.g.
        `https://mira.example/otlp/v1/traces`) — the wire format is OTLP;
        this module has no opinion on what sits behind it (an OTel
        Collector, a vendor backend, or a bare receiver during development).

        `token` is sent as `Authorization: Bearer <token>` — in the Mira
        ecosystem this is the same run-scoped credential used to
        authenticate to the MCP environment-access surface (see
        `mirarun`'s ADR-021), since both answer "which run is this."

        `span_exporter` overrides the OTLP exporter entirely (tests, or a
        non-OTLP destination) — when set, `endpoint`/`token` are ignored.
        """
        attributes: dict[str, Any] = {
            "service.name": service_name,
            "mira.run.id": run_id,
        }
        if service_version is not None:
            attributes["service.version"] = service_version
        if deployment_environment is not None:
            attributes["deployment.environment"] = deployment_environment
        if agent_id is not None:
            attributes["mira.agent.id"] = agent_id
        if routine_id is not None:
            attributes["mira.routine.id"] = routine_id
        if routine_revision_id is not None:
            attributes["mira.routine.revision.id"] = routine_revision_id
        if extra_resource_attributes:
            attributes.update(extra_resource_attributes)

        self._stats = ExportStats()
        delegate = span_exporter or OTLPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": f"Bearer {token}"} if token else None,
        )
        exporter = _CountingExporter(delegate, self._stats)
        self._provider = TracerProvider(resource=Resource.create(attributes))
        self._provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=max_queue_size,
                max_export_batch_size=max_export_batch_size,
                schedule_delay_millis=schedule_delay_millis,
                export_timeout_millis=export_timeout_millis,
            )
        )
        self._tracer = self._provider.get_tracer("mira_sdk")

    @property
    def export_stats(self) -> ExportStats:
        return self._stats

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        """Open a span under the current trace context.

        `None`-valued attributes are dropped rather than sent — callers can
        pass optional fields unconditionally (`target_uri=maybe_none`)
        without every call site needing its own filter.

        An exception raised inside the `with` block is recorded on the span
        and its status set to ERROR by `start_as_current_span`'s own default
        behavior (`record_exception=True`, `set_status_on_exception=True`)
        — not duplicated here — and always re-raised unchanged; a context
        manager never swallows an exception it doesn't itself catch.
        """
        with self._tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, value)
            yield span

    def shutdown(self, timeout_millis: int = 5000) -> None:
        """Flush queued spans (bounded by `timeout_millis`) and stop the
        background export thread.

        Call once, at process exit — not per-span or per-turn.
        `TracerProvider.shutdown()` itself takes no caller-controlled
        timeout in the OTel SDK (it always waits its own fixed internal
        default); the bound this method actually promises comes from
        calling `force_flush(timeout_millis)` first, so a hung/unreachable
        endpoint delays exit by at most that long, not OTel's default.
        """
        self._provider.force_flush(timeout_millis)
        self._provider.shutdown()


class _CountingExporter(SpanExporter):
    """Wraps a SpanExporter to maintain `ExportStats`. Delegates export
    behavior — including OTLPSpanExporter's own retry-with-backoff — to the
    wrapped exporter unchanged; this only observes the outcome."""

    def __init__(self, delegate: SpanExporter, stats: ExportStats) -> None:
        self._delegate = delegate
        self._stats = stats

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._stats.attempted_batches += 1
        try:
            result = self._delegate.export(spans)
        except Exception:
            # Exporters are documented to return FAILURE rather than raise;
            # this is a defensive backstop against one that doesn't, not
            # the expected path.
            logger.warning("mira_sdk telemetry export raised", exc_info=True)
            result = SpanExportResult.FAILURE
        if result != SpanExportResult.SUCCESS:
            self._stats.failed_batches += 1
            self._stats.failed_spans += len(spans)
        return result

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)
