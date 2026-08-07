"""The driver report loop (mirarun ADR-022's named missing piece).

Polls one `EnvironmentDriver` on a schedule, assembles a `DriverSnapshot` —
container inventory with details, optional stats-derived CPU figures,
optional host metrics, and lifecycle events detected between polls — and
hands it to every configured sink. Sinks are isolated from each other: one
sink failing must not starve the others, because the sinks feed different
systems (mirarun's report cache, movingfirm-admin's collector) whose
availability is unrelated.

Event detection is poll-based, not a subscription to the Docker events
stream: the runner compares each container's lifecycle fields against the
previous cycle. A container that dies *and is removed* between two polls is
missed — accepted, and worth stating: this loop observes state, it does not
promise a complete event history.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Protocol

from miraasdk.driver.base import (
    DriverContainerStats,
    DriverError,
    DriverResourceDetails,
    DriverResourceNotFound,
    DriverResourceQuery,
    EnvironmentDriver,
)
from miraasdk.driver.metrics import (
    HostMetricsCollector,
    HostMetricsUnavailable,
    MetricSample,
)

logger = logging.getLogger(__name__)

_MAX_POSTMORTEM_LINE_CHARS = 1000


@dataclass(frozen=True, slots=True)
class DriverEvent:
    kind: str  # container.oom_killed | container.exited | container.restarted
    uri: str
    occurred_at: str  # detection time (runner clock), not the death time
    exit_code: int | None = None
    log_tail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContainerObservation:
    details: DriverResourceDetails
    stats: DriverContainerStats | None = None
    # docker-CLI convention: percent of one CPU, so a busy 4-core container
    # reads 400.0. None until two cycles have sampled the same container.
    cpu_percent: float | None = None


@dataclass(frozen=True, slots=True)
class DriverSnapshot:
    target_reference: str
    collected_at: str
    containers: tuple[ContainerObservation, ...]
    host_metrics: tuple[MetricSample, ...] = ()
    events: tuple[DriverEvent, ...] = ()


class SnapshotSink(Protocol):
    def publish(self, snapshot: DriverSnapshot) -> None: ...


@dataclass(frozen=True, slots=True)
class _Baseline:
    restart_count: int | None
    finished_at: str | None
    stats: DriverContainerStats | None


class DriverRunner:
    def __init__(
        self,
        driver: EnvironmentDriver,
        sinks: Sequence[SnapshotSink],
        *,
        target_reference: str,
        host_metrics: HostMetricsCollector | None = None,
        interval_seconds: float = 60.0,
        postmortem_tail: int = 50,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not sinks:
            raise ValueError("a runner without sinks would poll into the void")
        self._driver = driver
        self._sinks = list(sinks)
        self._reference = target_reference
        self._host_metrics = host_metrics
        self._interval = interval_seconds
        self._postmortem_tail = postmortem_tail
        self._now = now or (lambda: datetime.now(timezone.utc))
        # `stats` is a DockerDriver extra, deliberately outside the
        # EnvironmentDriver protocol — probe for it rather than requiring it
        # so a stats-less driver still reports inventory and events.
        self._stats_supported = callable(getattr(driver, "stats", None))
        self._baselines: dict[str, _Baseline] = {}

    def run_once(self) -> DriverSnapshot:
        """One poll cycle: collect, then publish to every sink. Raises only
        when the cycle produced nothing publishable; sink failures are
        logged per sink and never interrupt the others."""
        snapshot = self._collect()
        for sink in self._sinks:
            try:
                sink.publish(snapshot)
            except Exception:
                logger.exception(
                    "sink %s failed to publish snapshot for %s",
                    type(sink).__name__,
                    self._reference,
                )
        return snapshot

    def run_forever(self, *, stop: threading.Event | None = None) -> None:
        stop_event = stop or threading.Event()
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except DriverError:
                # The infrastructure endpoint being down is an operational
                # condition to ride out, not a reason for the reporter to
                # die — the next cycle retries from scratch.
                logger.exception("poll cycle failed for %s", self._reference)
            elapsed = time.monotonic() - started
            stop_event.wait(max(0.0, self._interval - elapsed))

    def _collect(self) -> DriverSnapshot:
        collected_at = self._now().isoformat()
        resources = self._driver.query(DriverResourceQuery(resource_type="container"))
        observations: list[ContainerObservation] = []
        events: list[DriverEvent] = []
        new_baselines: dict[str, _Baseline] = {}
        failed = 0
        for resource in resources:
            try:
                details = self._driver.describe(resource.uri)
            except DriverResourceNotFound:
                # Vanished between list and inspect: confirmed absence, so
                # omitting it from the report is truthful.
                logger.info("container vanished mid-poll: %s", resource.uri)
                failed += 1
                continue
            except DriverError:
                # Any other failure is NOT evidence of absence — but both
                # receivers treat absence as authoritative (mirarun's report
                # cache replaces wholesale; admin's ingest deletes on
                # absence), so publishing without this container would
                # delete a possibly-live container from their inventories,
                # and dropping its baseline would blind lifecycle detection
                # when it reappears. Abort the whole cycle instead: nothing
                # publishes, baselines stay, the next cycle retries.
                raise
            stats = self._sample_stats(resource.uri)
            previous = self._baselines.get(resource.uri)
            cpu_percent = _cpu_percent(previous.stats if previous else None, stats)
            observations.append(
                ContainerObservation(details=details, stats=stats, cpu_percent=cpu_percent)
            )
            event = self._detect_event(previous, details, collected_at)
            if event is not None:
                events.append(event)
            new_baselines[resource.uri] = _Baseline(
                restart_count=details.restart_count,
                finished_at=details.finished_at,
                stats=stats,
            )
        if failed:
            # Only confirmed-vanished containers reach here (anything else
            # aborted the cycle above) — an empty inventory after N
            # not-founds is genuinely "they're all gone", not degradation.
            logger.warning(
                "%d of %d containers vanished mid-poll for %s",
                failed,
                len(resources),
                self._reference,
            )
        self._baselines = new_baselines
        host_metrics: tuple[MetricSample, ...] = ()
        if self._host_metrics is not None:
            try:
                host_metrics = self._host_metrics.collect()
            except HostMetricsUnavailable:
                # Container data is still worth publishing; the logged error
                # is the signal that host observation is broken.
                logger.exception("host metrics collection failed for %s", self._reference)
        return DriverSnapshot(
            target_reference=self._reference,
            collected_at=collected_at,
            containers=tuple(observations),
            host_metrics=host_metrics,
            events=tuple(events),
        )

    def _sample_stats(self, uri: str) -> DriverContainerStats | None:
        if not self._stats_supported:
            return None
        try:
            return self._driver.stats(uri)  # type: ignore[attr-defined]
        except DriverError:
            logger.exception("stats failed for %s", uri)
            return None

    def _detect_event(
        self,
        previous: _Baseline | None,
        details: DriverResourceDetails,
        occurred_at: str,
    ) -> DriverEvent | None:
        if previous is None:
            return None  # first sighting — no baseline, no transition
        died = (
            details.finished_at is not None
            and details.finished_at != previous.finished_at
        )
        restarted = (
            details.restart_count is not None
            and previous.restart_count is not None
            and details.restart_count > previous.restart_count
        )
        if not died and not restarted:
            return None
        # OOM takes precedence over the mechanical kind: whether the
        # container stayed down or was restarted, the OOM is the signal an
        # operator needs. Note OOMKilled resets when the container starts
        # again, so an OOM followed by a restart *before our next poll* can
        # surface as a plain restart — a stated limit of poll-based
        # detection, not a bug to fix here.
        if details.oom_killed:
            kind = "container.oom_killed"
        elif restarted:
            kind = "container.restarted"
        else:
            kind = "container.exited"
        return DriverEvent(
            kind=kind,
            uri=details.resource.uri,
            occurred_at=occurred_at,
            exit_code=details.exit_code,
            log_tail=self._postmortem(details.resource.uri),
        )

    def _postmortem(self, uri: str) -> tuple[str, ...]:
        try:
            result = self._driver.logs(uri, tail=self._postmortem_tail)
        except DriverError:
            # The whole point of the tail is a container that just died —
            # it may already be gone. An empty tail is honest.
            logger.exception("post-mortem log tail failed for %s", uri)
            return ()
        lines = []
        for entry in result.entries:
            prefix = f"{entry.timestamp} " if entry.timestamp else ""
            lines.append(f"{prefix}[{entry.stream}] {entry.text}"[:_MAX_POSTMORTEM_LINE_CHARS])
        return tuple(lines)


def _cpu_percent(
    previous: DriverContainerStats | None, current: DriverContainerStats | None
) -> float | None:
    if previous is None or current is None:
        return None
    if (
        previous.cpu_total_ns is None
        or previous.cpu_system_ns is None
        or current.cpu_total_ns is None
        or current.cpu_system_ns is None
    ):
        return None
    delta_total = current.cpu_total_ns - previous.cpu_total_ns
    delta_system = current.cpu_system_ns - previous.cpu_system_ns
    if delta_total < 0 or delta_system <= 0:
        # Counters reset — the container was recreated under the same
        # address. Re-baseline silently.
        return None
    cpus = current.online_cpus or 1
    return (delta_total / delta_system) * cpus * 100.0
