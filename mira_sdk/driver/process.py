"""The driver as a process: env-var configuration and the console entrypoint.

This module is deliberately the only place in mira-sdk that reads the
environment. Every class in the SDK stays constructor-configured — an
embedding application decides its own configuration story — but a *process*
has to get configuration from somewhere, and for a container under Compose
that somewhere is env vars.

Config surface (all prefixed MIRA_DRIVER_):

  TARGET_REFERENCE   required; the reference mirarun's registry knows this
                     host by (also the default admin node_id)
  DOCKER_ENDPOINT    default unix:///var/run/docker.sock; an http(s):// URL
                     reaches a TCP endpoint such as a docker-socket-proxy
  INTERVAL_SECONDS   default 60, minimum 5
  POSTMORTEM_TAIL    default 50, range 1-1000 (driver log-tail bounds)
  MIRARUN_REPORT_URL / MIRARUN_REPORT_TOKEN
                     optional pair; enables the mirarun report sink for
                     hosts registered with reachability "reported"
  ADMIN_COLLECTOR_URL / ADMIN_COLLECTOR_TOKEN
                     optional pair; enables the movingfirm-admin sink
  NODE_ID            default TARGET_REFERENCE
  NODE_HOST          default the process hostname
  NODE_ENV           optional deployment environment label (e.g. prod)
  HOST_METRICS       default true
  PROC_PATH          default /proc; point at a bind-mounted host procfs
                     (e.g. /host/proc) when containerised
  DISK_PATHS         default "/=/": comma-separated reported=probed pairs,
                     e.g. "/=/host/probes/rootfs" — the probed path only
                     needs statvfs, so a single bind-mounted file from the
                     target filesystem is enough

At least one sink pair must be configured: a driver with no sink polls into
the void, which is exactly the kind of silently-useless deployment that
should fail at startup instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import os
import signal
import socket
import sys
import threading

from mira_sdk.driver.docker import DockerDriver
from mira_sdk.driver.metrics import HostMetricsCollector
from mira_sdk.driver.runner import DriverRunner, SnapshotSink
from mira_sdk.driver.sinks import AdminCollectorSink, MirarunReportSink
from mira_sdk.driver.uri import valid_target_reference

logger = logging.getLogger(__name__)

_PREFIX = "MIRA_DRIVER_"


class DriverConfigError(Exception):
    """The environment does not describe a runnable driver process."""


@dataclass(frozen=True, slots=True)
class DriverProcessConfig:
    target_reference: str
    docker_endpoint: str
    interval_seconds: float
    postmortem_tail: int
    mirarun_report_url: str | None
    mirarun_report_token: str | None
    admin_collector_url: str | None
    admin_collector_token: str | None
    node_id: str
    node_host: str
    node_env: str | None
    host_metrics_enabled: bool
    proc_path: str
    disk_paths: tuple[tuple[str, str], ...]

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "DriverProcessConfig":
        def get(name: str, default: str | None = None) -> str | None:
            value = environ.get(f"{_PREFIX}{name}", default)
            if value is not None:
                value = value.strip()
            return value or default

        reference = get("TARGET_REFERENCE")
        if reference is None:
            raise DriverConfigError(f"{_PREFIX}TARGET_REFERENCE is required")
        if not valid_target_reference(reference):
            raise DriverConfigError(
                f"{_PREFIX}TARGET_REFERENCE is not a valid env:// target reference"
            )

        interval = _positive_float(get("INTERVAL_SECONDS", "60"), name="INTERVAL_SECONDS")
        if interval < 5:
            raise DriverConfigError(
                f"{_PREFIX}INTERVAL_SECONDS below 5 hammers the endpoint for no "
                "operational gain"
            )
        tail = _bounded_int(get("POSTMORTEM_TAIL", "50"), name="POSTMORTEM_TAIL")

        mirarun_url = get("MIRARUN_REPORT_URL")
        mirarun_token = get("MIRARUN_REPORT_TOKEN")
        admin_url = get("ADMIN_COLLECTOR_URL")
        admin_token = get("ADMIN_COLLECTOR_TOKEN")
        _require_pair(mirarun_url, mirarun_token, name="MIRARUN_REPORT")
        _require_pair(admin_url, admin_token, name="ADMIN_COLLECTOR")
        if mirarun_url is None and admin_url is None:
            raise DriverConfigError(
                "no sink configured: set at least one of "
                f"{_PREFIX}MIRARUN_REPORT_URL/_TOKEN or "
                f"{_PREFIX}ADMIN_COLLECTOR_URL/_TOKEN"
            )

        host_metrics_raw = (get("HOST_METRICS", "true") or "true").lower()
        if host_metrics_raw not in {"true", "false"}:
            raise DriverConfigError(f"{_PREFIX}HOST_METRICS must be true or false")

        return cls(
            target_reference=reference,
            docker_endpoint=get("DOCKER_ENDPOINT", "unix:///var/run/docker.sock"),  # type: ignore[arg-type]
            interval_seconds=interval,
            postmortem_tail=tail,
            mirarun_report_url=mirarun_url,
            mirarun_report_token=mirarun_token,
            admin_collector_url=admin_url,
            admin_collector_token=admin_token,
            node_id=get("NODE_ID", reference),  # type: ignore[arg-type]
            node_host=get("NODE_HOST", socket.gethostname()),  # type: ignore[arg-type]
            node_env=get("NODE_ENV"),
            host_metrics_enabled=host_metrics_raw == "true",
            proc_path=get("PROC_PATH", "/proc"),  # type: ignore[arg-type]
            disk_paths=_parse_disk_paths(get("DISK_PATHS", "/=/")),  # type: ignore[arg-type]
        )


def _positive_float(value: str | None, *, name: str) -> float:
    try:
        parsed = float(value or "")
    except ValueError as error:
        raise DriverConfigError(f"{_PREFIX}{name} must be a number") from error
    if parsed <= 0:
        raise DriverConfigError(f"{_PREFIX}{name} must be positive")
    return parsed


def _bounded_int(value: str | None, *, name: str) -> int:
    try:
        parsed = int(value or "")
    except ValueError as error:
        raise DriverConfigError(f"{_PREFIX}{name} must be an integer") from error
    if parsed < 1 or parsed > 1000:
        raise DriverConfigError(f"{_PREFIX}{name} must be between 1 and 1000")
    return parsed


def _require_pair(url: str | None, token: str | None, *, name: str) -> None:
    if (url is None) != (token is None):
        raise DriverConfigError(
            f"{_PREFIX}{name}_URL and {_PREFIX}{name}_TOKEN must be set together"
        )


def _parse_disk_paths(value: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        reported, separator, probed = item.partition("=")
        if not separator:
            probed = reported  # bare path: report it under its own name
        if not reported or not probed:
            raise DriverConfigError(
                f"{_PREFIX}DISK_PATHS entries must be 'reported=probed' pairs"
            )
        pairs.append((reported, probed))
    return tuple(pairs)


def build_runner(config: DriverProcessConfig) -> DriverRunner:
    driver = DockerDriver(config.target_reference, endpoint=config.docker_endpoint)
    sinks: list[SnapshotSink] = []
    if config.mirarun_report_url is not None and config.mirarun_report_token is not None:
        sinks.append(
            MirarunReportSink(
                report_url=config.mirarun_report_url,
                token=config.mirarun_report_token,
            )
        )
    if config.admin_collector_url is not None and config.admin_collector_token is not None:
        sinks.append(
            AdminCollectorSink(
                collector_url=config.admin_collector_url,
                token=config.admin_collector_token,
                node_id=config.node_id,
                host=config.node_host,
                environment=config.node_env,
            )
        )
    host_metrics = (
        HostMetricsCollector(
            proc_path=config.proc_path,
            disk_paths=dict(config.disk_paths),
        )
        if config.host_metrics_enabled
        else None
    )
    return DriverRunner(
        driver,
        sinks,
        target_reference=config.target_reference,
        host_metrics=host_metrics,
        interval_seconds=config.interval_seconds,
        postmortem_tail=config.postmortem_tail,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = DriverProcessConfig.from_environ(os.environ)
    except DriverConfigError as error:
        print(f"mira-driver: {error}", file=sys.stderr)
        return 2
    runner = build_runner(config)
    stop = threading.Event()

    def _terminate(signum: int, _frame: object) -> None:
        logger.info("received signal %d, stopping after the current cycle", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    logger.info(
        "mira-driver polling %s every %.0fs (sinks: mirarun=%s admin=%s host_metrics=%s)",
        config.target_reference,
        config.interval_seconds,
        "on" if config.mirarun_report_url else "off",
        "on" if config.admin_collector_url else "off",
        "on" if config.host_metrics_enabled else "off",
    )
    runner.run_forever(stop=stop)
    return 0
