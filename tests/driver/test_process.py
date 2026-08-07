from __future__ import annotations

import pytest

from mira_sdk.driver.process import (
    DriverConfigError,
    DriverProcessConfig,
    build_runner,
)
from mira_sdk.driver.runner import DriverRunner

_MINIMAL = {
    "MIRA_DRIVER_TARGET_REFERENCE": "vps1",
    "MIRA_DRIVER_ADMIN_COLLECTOR_URL": "http://10.8.0.4:6767/api/ops/infra/collector",
    "MIRA_DRIVER_ADMIN_COLLECTOR_TOKEN": "collector-token",
}


def test_minimal_environ_gets_defaults():
    config = DriverProcessConfig.from_environ(_MINIMAL)
    assert config.target_reference == "vps1"
    assert config.docker_endpoint == "unix:///var/run/docker.sock"
    assert config.interval_seconds == 60
    assert config.postmortem_tail == 50
    assert config.node_id == "vps1"  # defaults to the target reference
    assert config.node_env is None
    assert config.host_metrics_enabled is True
    assert config.proc_path == "/proc"
    assert config.disk_paths == (("/", "/"),)
    assert config.mirarun_report_url is None


def test_target_reference_is_required_and_validated():
    with pytest.raises(DriverConfigError):
        DriverProcessConfig.from_environ({})
    with pytest.raises(DriverConfigError):
        # Uppercase is invalid in the env:// grammar — catching it at
        # startup beats every produced URI failing to parse downstream.
        DriverProcessConfig.from_environ({**_MINIMAL, "MIRA_DRIVER_TARGET_REFERENCE": "VPS1"})


def test_sink_pairs_must_be_complete():
    environ = dict(_MINIMAL)
    del environ["MIRA_DRIVER_ADMIN_COLLECTOR_TOKEN"]
    with pytest.raises(DriverConfigError):
        DriverProcessConfig.from_environ(environ)


def test_at_least_one_sink_is_required():
    with pytest.raises(DriverConfigError):
        DriverProcessConfig.from_environ({"MIRA_DRIVER_TARGET_REFERENCE": "vps1"})


def test_interval_floor():
    with pytest.raises(DriverConfigError):
        DriverProcessConfig.from_environ({**_MINIMAL, "MIRA_DRIVER_INTERVAL_SECONDS": "1"})


def test_postmortem_tail_bounds_match_the_driver_log_bounds():
    with pytest.raises(DriverConfigError):
        DriverProcessConfig.from_environ({**_MINIMAL, "MIRA_DRIVER_POSTMORTEM_TAIL": "1001"})


def test_disk_paths_parse_reported_and_probed_forms():
    config = DriverProcessConfig.from_environ(
        {**_MINIMAL, "MIRA_DRIVER_DISK_PATHS": "/=/host/probes/rootfs,/var/lib/docker"}
    )
    assert config.disk_paths == (
        ("/", "/host/probes/rootfs"),
        ("/var/lib/docker", "/var/lib/docker"),  # bare path probes itself
    )


def test_blank_values_are_treated_as_unset():
    # Compose renders undefined variables as empty strings
    # (`${VAR:-}` → ""); an empty URL must mean "sink off", not a sink
    # pointed at "".
    config = DriverProcessConfig.from_environ(
        {**_MINIMAL, "MIRA_DRIVER_MIRARUN_REPORT_URL": "", "MIRA_DRIVER_MIRARUN_REPORT_TOKEN": ""}
    )
    assert config.mirarun_report_url is None
    assert config.mirarun_report_token is None


def test_build_runner_smoke():
    runner = build_runner(DriverProcessConfig.from_environ(_MINIMAL))
    assert isinstance(runner, DriverRunner)
