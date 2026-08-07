from __future__ import annotations

from datetime import datetime, timezone

import pytest

from miraasdk.driver.base import (
    DriverUnavailable,
    DriverContainerStats,
    DriverError,
    DriverLogEntry,
    DriverLogResult,
    DriverResource,
    DriverResourceDetails,
    DriverResourceNotFound,
)
from miraasdk.driver.metrics import HostMetricsUnavailable, MetricSample
from miraasdk.driver.runner import DriverRunner

API_URI = "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1"
WORKER_URI = (
    "env://vps1/docker/project/muutto365/service/worker/container/muutto365-worker-1"
)


def _details(
    uri: str,
    name: str,
    *,
    state: str = "running",
    restart_count: int | None = 0,
    finished_at: str | None = None,
    exit_code: int | None = None,
    oom_killed: bool | None = False,
) -> DriverResourceDetails:
    return DriverResourceDetails(
        resource=DriverResource(
            uri=uri,
            resource_type="container",
            name=name,
            project="muutto365",
            service=name.split("-")[1],
            state=state,
            image=f"muutto365/{name.split('-')[1]}:latest",
        ),
        created_at="2026-01-01T00:00:00Z",
        restart_count=restart_count,
        finished_at=finished_at,
        exit_code=exit_code,
        oom_killed=oom_killed,
    )


def _stats(uri: str, *, total_ns: int, system_ns: int, cpus: int = 2) -> DriverContainerStats:
    return DriverContainerStats(
        uri=uri, cpu_total_ns=total_ns, cpu_system_ns=system_ns, online_cpus=cpus
    )


class FakeDriver:
    """Scripted EnvironmentDriver + stats. Tests mutate `containers` between
    cycles to simulate lifecycle transitions."""

    def __init__(self) -> None:
        self.containers: dict[str, DriverResourceDetails] = {}
        self.stats_by_uri: dict[str, DriverContainerStats] = {}
        self.describe_failures: dict[str, Exception] = {}
        self.log_lines: dict[str, list[DriverLogEntry]] = {}
        self.log_requests: list[str] = []

    def query(self, query):
        return [details.resource for details in self.containers.values()]

    def describe(self, uri: str) -> DriverResourceDetails:
        if uri in self.describe_failures:
            raise self.describe_failures[uri]
        return self.containers[uri]

    def logs(self, uri: str, *, tail: int) -> DriverLogResult:
        self.log_requests.append(uri)
        return DriverLogResult(entries=tuple(self.log_lines.get(uri, ())), truncated=False)

    def stats(self, uri: str) -> DriverContainerStats:
        if uri not in self.stats_by_uri:
            raise DriverResourceNotFound("no stats")
        return self.stats_by_uri[uri]


class RecordingSink:
    def __init__(self) -> None:
        self.snapshots = []

    def publish(self, snapshot) -> None:
        self.snapshots.append(snapshot)


class ExplodingSink:
    def publish(self, snapshot) -> None:
        raise RuntimeError("sink is down")


def _runner(driver, sinks, **kwargs):
    kwargs.setdefault("target_reference", "vps1")
    kwargs.setdefault("now", lambda: datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc))
    return DriverRunner(driver, sinks, **kwargs)


def test_cycle_publishes_inventory_with_details():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    sink = RecordingSink()
    snapshot = _runner(driver, [sink]).run_once()
    assert sink.snapshots == [snapshot]
    assert snapshot.target_reference == "vps1"
    assert snapshot.collected_at == "2026-08-07T12:00:00+00:00"
    assert [obs.details.resource.uri for obs in snapshot.containers] == [API_URI]


def test_cpu_percent_needs_two_cycles_and_uses_docker_cli_convention():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    driver.stats_by_uri[API_URI] = _stats(API_URI, total_ns=1_000_000_000, system_ns=10_000_000_000)
    sink = RecordingSink()
    runner = _runner(driver, [sink])

    first = runner.run_once()
    assert first.containers[0].cpu_percent is None  # baseline cycle

    driver.stats_by_uri[API_URI] = _stats(API_URI, total_ns=2_000_000_000, system_ns=20_000_000_000)
    second = runner.run_once()
    # docker-CLI convention: (Δtotal/Δsystem) × cpus × 100 → 20% here.
    assert second.containers[0].cpu_percent == pytest.approx(20.0)


def test_cpu_percent_rebaselines_when_counters_reset():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    driver.stats_by_uri[API_URI] = _stats(API_URI, total_ns=9_000_000_000, system_ns=90_000_000_000)
    runner = _runner(driver, [RecordingSink()])
    runner.run_once()
    # Container recreated under the same address: counters restart from
    # near zero. A negative delta must yield None, not a huge bogus figure.
    driver.stats_by_uri[API_URI] = _stats(API_URI, total_ns=100, system_ns=1_000)
    snapshot = runner.run_once()
    assert snapshot.containers[0].cpu_percent is None


def test_death_between_cycles_emits_event_with_postmortem_tail():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    driver.log_lines[API_URI] = [
        DriverLogEntry(stream="stderr", text="MemoryError", timestamp="2026-08-07T11:59:58Z")
    ]
    sink = RecordingSink()
    runner = _runner(driver, [sink])
    runner.run_once()

    driver.containers[API_URI] = _details(
        API_URI,
        "muutto365-api-1",
        state="exited",
        finished_at="2026-08-07T11:59:59Z",
        exit_code=137,
        oom_killed=True,
    )
    snapshot = runner.run_once()
    assert len(snapshot.events) == 1
    event = snapshot.events[0]
    assert event.kind == "container.oom_killed"
    assert event.uri == API_URI
    assert event.exit_code == 137
    assert event.log_tail == ("2026-08-07T11:59:58Z [stderr] MemoryError",)


def test_restart_count_increase_emits_restarted_event():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1", restart_count=1)
    runner = _runner(driver, [RecordingSink()])
    runner.run_once()

    driver.containers[API_URI] = _details(
        API_URI,
        "muutto365-api-1",
        restart_count=2,
        finished_at="2026-08-07T11:59:59Z",
        exit_code=1,
    )
    snapshot = runner.run_once()
    assert [event.kind for event in snapshot.events] == ["container.restarted"]


def test_first_sighting_emits_no_event():
    driver = FakeDriver()
    # Already dead when first observed: there is no baseline to compare
    # against, so no transition can honestly be claimed.
    driver.containers[API_URI] = _details(
        API_URI, "muutto365-api-1", state="exited",
        finished_at="2026-08-07T11:00:00Z", exit_code=1,
    )
    snapshot = _runner(driver, [RecordingSink()]).run_once()
    assert snapshot.events == ()


def test_one_failing_sink_does_not_starve_the_other():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    healthy = RecordingSink()
    runner = _runner(driver, [ExplodingSink(), healthy])
    runner.run_once()
    assert len(healthy.snapshots) == 1


def test_confirmed_vanished_container_is_omitted_and_the_rest_publish():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    driver.containers[WORKER_URI] = _details(WORKER_URI, "muutto365-worker-1")
    # A 404 between list and inspect is confirmed absence — omitting the
    # container is truthful, so the rest of the report still goes out.
    driver.describe_failures[WORKER_URI] = DriverResourceNotFound("vanished mid-poll")
    sink = RecordingSink()
    _runner(driver, [sink]).run_once()
    uris = [obs.details.resource.uri for obs in sink.snapshots[0].containers]
    assert uris == [API_URI]


def test_transient_describe_failure_aborts_the_cycle_and_keeps_baselines():
    # A DriverUnavailable is NOT evidence of absence, and both receivers
    # treat absence as authoritative (mirarun replaces wholesale, admin
    # deletes on absence) — publishing without the container would delete
    # a possibly-live one. Nothing may publish, and the baseline must
    # survive so lifecycle detection still works when the target recovers
    # (Codex review on PR #2).
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    driver.containers[WORKER_URI] = _details(WORKER_URI, "muutto365-worker-1")
    sink = RecordingSink()
    runner = _runner(driver, [sink])
    runner.run_once()  # healthy baseline cycle
    assert len(sink.snapshots) == 1

    driver.describe_failures[WORKER_URI] = DriverUnavailable("daemon hiccup")
    with pytest.raises(DriverUnavailable):
        runner.run_once()
    assert len(sink.snapshots) == 1  # the degraded cycle published nothing

    # Recovery: the worker dies for real. Its baseline survived the aborted
    # cycle, so the death is still detected as a transition.
    del driver.describe_failures[WORKER_URI]
    driver.containers[WORKER_URI] = _details(
        WORKER_URI,
        "muutto365-worker-1",
        state="exited",
        finished_at="2026-08-07T12:05:00Z",
        exit_code=1,
    )
    snapshot = runner.run_once()
    assert [event.kind for event in snapshot.events] == ["container.exited"]


def test_all_containers_vanishing_publishes_an_empty_truthful_inventory():
    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    driver.describe_failures[API_URI] = DriverResourceNotFound("gone")
    sink = RecordingSink()
    _runner(driver, [sink]).run_once()
    # Every skip was a confirmed 404: the host genuinely runs nothing that
    # the list call saw, so the empty report is honest — unlike the
    # transient-failure case above, which aborts instead.
    assert sink.snapshots[0].containers == ()


def test_genuinely_empty_inventory_is_published():
    driver = FakeDriver()
    sink = RecordingSink()
    _runner(driver, [sink]).run_once()
    assert sink.snapshots[0].containers == ()


def test_host_metrics_failure_degrades_to_container_data_only():
    class BrokenCollector:
        def collect(self):
            raise HostMetricsUnavailable("proc_path points at nothing")

    driver = FakeDriver()
    driver.containers[API_URI] = _details(API_URI, "muutto365-api-1")
    sink = RecordingSink()
    _runner(driver, [sink], host_metrics=BrokenCollector()).run_once()
    snapshot = sink.snapshots[0]
    assert snapshot.host_metrics == ()
    assert len(snapshot.containers) == 1


def test_host_metrics_are_included_when_available():
    class StubCollector:
        def collect(self):
            return (MetricSample(name="system.uptime", value=42.0, unit="s"),)

    driver = FakeDriver()
    sink = RecordingSink()
    _runner(driver, [sink], host_metrics=StubCollector()).run_once()
    assert sink.snapshots[0].host_metrics[0].name == "system.uptime"


def test_runner_requires_at_least_one_sink():
    with pytest.raises(ValueError):
        _runner(FakeDriver(), [])
