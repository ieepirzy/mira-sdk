from __future__ import annotations

import os
import types

import pytest

from mira_sdk.driver.metrics import HostMetricsCollector, HostMetricsUnavailable


def _write_proc(
    tmp_path,
    *,
    stat_cpu: str = "cpu  100 0 100 700 100 0 0 0 0 0",
    meminfo: str | None = None,
) -> str:
    (tmp_path / "loadavg").write_text("0.52 0.40 0.30 1/234 5678\n")
    (tmp_path / "stat").write_text(
        f"{stat_cpu}\ncpu0 50 0 50 350 50 0 0 0 0 0\nintr 12345\n"
    )
    (tmp_path / "meminfo").write_text(
        meminfo
        or (
            "MemTotal:       16000000 kB\n"
            "MemFree:         2000000 kB\n"
            "MemAvailable:    6000000 kB\n"
            "Buffers:          500000 kB\n"
            "Cached:          3000000 kB\n"
        )
    )
    (tmp_path / "uptime").write_text("12345.67 23456.78\n")
    return str(tmp_path)


def _by_name(samples):
    index = {}
    for sample in samples:
        index[(sample.name, sample.attributes)] = sample
    return index


def _fake_statvfs(monkeypatch, *, frsize=4096, blocks=1_000_000, bfree=400_000, bavail=350_000):
    result = types.SimpleNamespace(
        f_frsize=frsize, f_blocks=blocks, f_bfree=bfree, f_bavail=bavail
    )
    monkeypatch.setattr(os, "statvfs", lambda path: result)
    return result


def test_load_average_and_uptime(tmp_path, monkeypatch):
    _fake_statvfs(monkeypatch)
    collector = HostMetricsCollector(proc_path=_write_proc(tmp_path))
    samples = _by_name(collector.collect())
    assert samples[("system.cpu.load_average.1m", ())].value == 0.52
    assert samples[("system.cpu.load_average.5m", ())].value == 0.40
    assert samples[("system.cpu.load_average.15m", ())].value == 0.30
    assert samples[("system.uptime", ())].value == 12345.67
    assert samples[("system.uptime", ())].unit == "s"


def test_cpu_utilization_needs_two_samples(tmp_path, monkeypatch):
    _fake_statvfs(monkeypatch)
    proc = _write_proc(tmp_path)
    collector = HostMetricsCollector(proc_path=proc)
    first = _by_name(collector.collect())
    # A utilization is a rate over two cumulative samples; the first call
    # can only establish the baseline.
    assert ("system.cpu.utilization", ()) not in first

    # t1: total=1000, idle+iowait=800. t2: total=1800, idle+iowait=1400.
    # busy delta 200 over total delta 800 → 0.25.
    _write_proc(tmp_path, stat_cpu="cpu  200 0 200 1300 100 0 0 0 0 0")
    second = _by_name(collector.collect())
    assert second[("system.cpu.utilization", ())].value == pytest.approx(0.25)


def test_cpu_counter_going_backwards_rebaselines_silently(tmp_path, monkeypatch):
    _fake_statvfs(monkeypatch)
    proc = _write_proc(tmp_path)
    collector = HostMetricsCollector(proc_path=proc)
    collector.collect()
    # Host rebooted: counters below the baseline. A ratio computed across
    # a reboot is nonsense — the sample must be withheld, not clamped.
    _write_proc(tmp_path, stat_cpu="cpu  10 0 10 70 10 0 0 0 0 0")
    samples = _by_name(collector.collect())
    assert ("system.cpu.utilization", ()) not in samples


def test_memory_used_is_total_minus_available(tmp_path, monkeypatch):
    _fake_statvfs(monkeypatch)
    collector = HostMetricsCollector(proc_path=_write_proc(tmp_path))
    samples = _by_name(collector.collect())
    total = 16_000_000 * 1024
    available = 6_000_000 * 1024
    used = samples[("system.memory.usage", (("state", "used"),))]
    assert used.value == float(total - available)
    assert used.unit == "By"
    assert samples[("system.memory.limit", ())].value == float(total)
    assert samples[("system.memory.utilization", ())].value == pytest.approx(
        (total - available) / total
    )


def test_filesystem_usage_reports_the_configured_mountpoint_name(tmp_path, monkeypatch):
    # The probed path (a bind-mounted file inside the container) and the
    # reported mountpoint (what the host calls the filesystem) differ by
    # design — consumers must see the host's name.
    _fake_statvfs(monkeypatch, frsize=4096, blocks=1_000_000, bfree=400_000, bavail=350_000)
    collector = HostMetricsCollector(
        proc_path=_write_proc(tmp_path),
        disk_paths={"/": "/host/probes/rootfs"},
    )
    samples = _by_name(collector.collect())
    used = samples[("system.filesystem.usage", (("mountpoint", "/"), ("state", "used")))]
    free = samples[("system.filesystem.usage", (("mountpoint", "/"), ("state", "free")))]
    assert used.value == float((1_000_000 - 400_000) * 4096)
    assert free.value == float(350_000 * 4096)
    utilization = samples[("system.filesystem.utilization", (("mountpoint", "/"),))]
    assert utilization.value == pytest.approx(used.value / (used.value + free.value))


def test_unreadable_proc_raises(tmp_path, monkeypatch):
    _fake_statvfs(monkeypatch)
    collector = HostMetricsCollector(proc_path=str(tmp_path / "missing"))
    with pytest.raises(HostMetricsUnavailable):
        collector.collect()


def test_malformed_meminfo_raises(tmp_path, monkeypatch):
    _fake_statvfs(monkeypatch)
    proc = _write_proc(tmp_path, meminfo="MemTotal: not-a-number kB\n")
    collector = HostMetricsCollector(proc_path=proc)
    with pytest.raises(HostMetricsUnavailable):
        collector.collect()
