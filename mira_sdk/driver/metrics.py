"""Host-level metrics read from procfs and the filesystem.

Metric names and units follow the OpenTelemetry semantic conventions
(`system.cpu.utilization`, `system.memory.usage`, ...) so that every
consumer — today movingfirm-admin's collector endpoint, later a real OTLP
pipeline — shares one vocabulary and nothing needs renaming when the wire
format upgrades. Attribute-bearing metrics carry their attributes
structurally; how they flatten onto a wire that has no attribute slot is a
sink decision, not made here.

The collector reads `/proc` by path so a containerised driver can point it
at a bind-mounted host procfs (`/host/proc`). Filesystem usage takes a
mapping of *reported* mountpoint to *probed* path for the same reason — the
probe only needs `statvfs`, so mounting a single innocuous file from the
target filesystem (e.g. `/etc/hostname`) is enough; the driver never needs
read access to the filesystem's contents.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os


class HostMetricsUnavailable(Exception):
    """The host's procfs could not be read or parsed."""


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    value: float
    unit: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()


class HostMetricsCollector:
    """Stateful: CPU utilization is a rate over cumulative counters, so the
    first `collect()` establishes a baseline and reports no utilization
    sample; every later call reports the utilization since the previous
    call. All other metrics are point-in-time gauges."""

    def __init__(
        self,
        *,
        proc_path: str = "/proc",
        disk_paths: Mapping[str, str] | None = None,
    ) -> None:
        self._proc = proc_path.rstrip("/")
        self._disks = dict(disk_paths) if disk_paths is not None else {"/": "/"}
        self._previous_cpu: tuple[int, int] | None = None

    def collect(self) -> tuple[MetricSample, ...]:
        samples: list[MetricSample] = []
        samples.extend(self._load_average())
        samples.extend(self._cpu_utilization())
        samples.extend(self._memory())
        samples.extend(self._uptime())
        samples.extend(self._filesystems())
        return tuple(samples)

    def _read(self, name: str) -> str:
        path = f"{self._proc}/{name}"
        try:
            with open(path, encoding="ascii", errors="replace") as handle:
                # procfs files are small; 64 KiB is far beyond any real
                # loadavg/stat/meminfo/uptime and bounds a misconfigured
                # proc_path pointed at something that isn't procfs.
                return handle.read(64 * 1024)
        except OSError as error:
            raise HostMetricsUnavailable(f"cannot read {path}") from error

    def _load_average(self) -> list[MetricSample]:
        fields = self._read("loadavg").split()
        if len(fields) < 3:
            raise HostMetricsUnavailable("loadavg is malformed")
        try:
            values = [float(field) for field in fields[:3]]
        except ValueError as error:
            raise HostMetricsUnavailable("loadavg is malformed") from error
        return [
            MetricSample(name=f"system.cpu.load_average.{window}", value=value, unit="1")
            for window, value in zip(("1m", "5m", "15m"), values)
        ]

    def _cpu_utilization(self) -> list[MetricSample]:
        for line in self._read("stat").splitlines():
            if line.startswith("cpu "):
                fields = line.split()[1:]
                break
        else:
            raise HostMetricsUnavailable("stat has no aggregate cpu line")
        try:
            jiffies = [int(field) for field in fields[:8]]
        except ValueError as error:
            raise HostMetricsUnavailable("stat cpu line is malformed") from error
        if len(jiffies) < 5:
            raise HostMetricsUnavailable("stat cpu line is malformed")
        # user nice system idle iowait [irq softirq steal]; idle time is
        # idle + iowait — the conventional reading (a CPU waiting on IO is
        # not doing work).
        total = sum(jiffies)
        idle = jiffies[3] + jiffies[4]
        previous, self._previous_cpu = self._previous_cpu, (total, idle)
        if previous is None:
            return []
        delta_total = total - previous[0]
        delta_idle = idle - previous[1]
        if delta_total <= 0 or delta_idle < 0 or delta_idle > delta_total:
            # Counter went backwards (reboot, proc_path swap) — re-baseline
            # rather than reporting a nonsense ratio.
            return []
        utilization = (delta_total - delta_idle) / delta_total
        return [MetricSample(name="system.cpu.utilization", value=utilization, unit="1")]

    def _memory(self) -> list[MetricSample]:
        values: dict[str, int] = {}
        for line in self._read("meminfo").splitlines():
            key, _, rest = line.partition(":")
            fields = rest.split()
            if key in {"MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached"} and fields:
                try:
                    values[key] = int(fields[0]) * 1024  # meminfo reports kB
                except ValueError as error:
                    raise HostMetricsUnavailable("meminfo is malformed") from error
        if "MemTotal" not in values or "MemAvailable" not in values:
            raise HostMetricsUnavailable("meminfo lacks MemTotal/MemAvailable")
        total = values["MemTotal"]
        available = values["MemAvailable"]
        # "used" is total minus *available* (not free): free excludes memory
        # the kernel would reclaim on demand, which is the number that
        # matters for "is this host running out".
        used = max(total - available, 0)
        samples = [
            MetricSample(
                name="system.memory.usage", value=float(used), unit="By",
                attributes=(("state", "used"),),
            ),
            MetricSample(
                name="system.memory.usage", value=float(available), unit="By",
                attributes=(("state", "available"),),
            ),
            MetricSample(name="system.memory.limit", value=float(total), unit="By"),
        ]
        if total > 0:
            samples.append(
                MetricSample(name="system.memory.utilization", value=used / total, unit="1")
            )
        return samples

    def _uptime(self) -> list[MetricSample]:
        fields = self._read("uptime").split()
        if not fields:
            raise HostMetricsUnavailable("uptime is malformed")
        try:
            seconds = float(fields[0])
        except ValueError as error:
            raise HostMetricsUnavailable("uptime is malformed") from error
        return [MetricSample(name="system.uptime", value=seconds, unit="s")]

    def _filesystems(self) -> list[MetricSample]:
        samples: list[MetricSample] = []
        for mountpoint, probe_path in sorted(self._disks.items()):
            try:
                stats = os.statvfs(probe_path)
            except OSError as error:
                raise HostMetricsUnavailable(
                    f"cannot statvfs {probe_path} (for mountpoint {mountpoint})"
                ) from error
            block = stats.f_frsize
            total = stats.f_blocks * block
            # f_bavail, not f_bfree: what an unprivileged writer can actually
            # use — the root-reserved blocks are not headroom.
            free = stats.f_bavail * block
            used = max(total - stats.f_bfree * block, 0)
            attributes = (("mountpoint", mountpoint),)
            samples.append(
                MetricSample(
                    name="system.filesystem.usage", value=float(used), unit="By",
                    attributes=(("mountpoint", mountpoint), ("state", "used")),
                )
            )
            samples.append(
                MetricSample(
                    name="system.filesystem.usage", value=float(free), unit="By",
                    attributes=(("mountpoint", mountpoint), ("state", "free")),
                )
            )
            if used + free > 0:
                samples.append(
                    MetricSample(
                        name="system.filesystem.utilization",
                        value=used / (used + free),
                        unit="1",
                        attributes=attributes,
                    )
                )
        return samples
