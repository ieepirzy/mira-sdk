from __future__ import annotations

from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from miraasdk import ExportStats, MiraTelemetry


def _telemetry(exporter: SpanExporter, **kwargs) -> MiraTelemetry:
    return MiraTelemetry(
        endpoint="http://unused.invalid/v1/traces",
        run_id="run-1",
        span_exporter=exporter,
        **kwargs,
    )


def test_resource_attributes_set_once_and_present_on_every_span():
    exporter = InMemorySpanExporter()
    telemetry = _telemetry(
        exporter,
        agent_id="agent-1",
        routine_id="routine-1",
        routine_revision_id="revision-1",
        service_version="1.2.3",
        deployment_environment="prod",
        extra_resource_attributes={"mira.environment.id": "vps1"},
    )
    with telemetry.span("turn.one"):
        pass
    with telemetry.span("turn.two"):
        pass
    telemetry.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        resource = span.resource.attributes
        assert resource["mira.run.id"] == "run-1"
        assert resource["mira.agent.id"] == "agent-1"
        assert resource["mira.routine.id"] == "routine-1"
        assert resource["mira.routine.revision.id"] == "revision-1"
        assert resource["service.name"] == "mira-agent"
        assert resource["service.version"] == "1.2.3"
        assert resource["deployment.environment"] == "prod"
        assert resource["mira.environment.id"] == "vps1"


def test_omitted_identity_fields_are_absent_not_null():
    exporter = InMemorySpanExporter()
    telemetry = _telemetry(exporter)
    with telemetry.span("op"):
        pass
    telemetry.shutdown()

    resource = exporter.get_finished_spans()[0].resource.attributes
    assert "mira.agent.id" not in resource
    assert "mira.routine.id" not in resource
    assert "mira.routine.revision.id" not in resource
    assert "service.version" not in resource
    assert "deployment.environment" not in resource


def test_span_sets_call_attributes_and_drops_none_values():
    exporter = InMemorySpanExporter()
    telemetry = _telemetry(exporter)
    with telemetry.span(
        "resource.query",
        **{"mira.resource.uri": "env://vps1/docker/project/x", "mira.capability": None},
    ):
        pass
    telemetry.shutdown()

    span = exporter.get_finished_spans()[0]
    assert span.name == "resource.query"
    assert span.attributes["mira.resource.uri"] == "env://vps1/docker/project/x"
    assert "mira.capability" not in span.attributes


def test_span_records_exception_exactly_once_and_reraises():
    exporter = InMemorySpanExporter()
    telemetry = _telemetry(exporter)

    with pytest.raises(ValueError, match="boom"):
        with telemetry.span("failing.op"):
            raise ValueError("boom")
    telemetry.shutdown()

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    exception_events = [e for e in span.events if e.name == "exception"]
    # start_as_current_span already records the exception by default
    # (record_exception=True); span() must not duplicate that itself.
    assert len(exception_events) == 1


def test_shutdown_calls_force_flush_with_the_given_timeout():
    # TracerProvider.shutdown() itself takes no caller-controlled timeout —
    # it always waits its own fixed internal default. The bound
    # MiraTelemetry.shutdown(timeout_millis=...) promises has to come from
    # calling provider.force_flush(timeout_millis) first; spy on the real
    # TracerProvider's own method (not the exporter — force_flush does not
    # call the exporter's force_flush(), it drains the internal queue via
    # export() on its own fixed export_timeout_millis, independent of this
    # value) to verify this module actually does that.
    telemetry = _telemetry(InMemorySpanExporter())
    calls: list[int] = []
    original = telemetry._provider.force_flush

    def spy(timeout_millis: int = 30000) -> bool:
        calls.append(timeout_millis)
        return original(timeout_millis)

    telemetry._provider.force_flush = spy
    telemetry.shutdown(timeout_millis=1234)

    assert calls == [1234]


def test_export_stats_track_failed_batches():
    class AlwaysFails(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    telemetry = _telemetry(AlwaysFails())
    with telemetry.span("op"):
        pass
    telemetry.shutdown()

    stats = telemetry.export_stats
    assert isinstance(stats, ExportStats)
    assert stats.attempted_batches == 1
    assert stats.failed_batches == 1
    assert stats.failed_spans == 1


def test_export_stats_track_exporter_raising_without_propagating():
    class Explodes(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            raise RuntimeError("network is on fire")

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    telemetry = _telemetry(Explodes())
    # The whole point: an operation's own success must not depend on
    # telemetry export succeeding.
    with telemetry.span("op"):
        pass
    telemetry.shutdown()

    assert telemetry.export_stats.failed_batches == 1


def test_successful_export_does_not_count_as_failed():
    exporter = InMemorySpanExporter()
    telemetry = _telemetry(exporter)
    with telemetry.span("op"):
        pass
    telemetry.shutdown()

    stats = telemetry.export_stats
    assert stats.attempted_batches == 1
    assert stats.failed_batches == 0
    assert stats.failed_spans == 0
