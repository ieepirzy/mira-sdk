from __future__ import annotations

import json
import struct

import httpx
import pytest

from mira_sdk.driver.base import (
    DriverOperationInvalid,
    DriverProtocolError,
    DriverResourceNotFound,
    DriverResourceQuery,
    DriverUnavailable,
    EnvironmentDriver,
)
from mira_sdk.driver.docker import DockerDriver

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
API_ID = "a" * 64
WORKER_ID = "b" * 64
ORPHAN_ID = "c" * 64

_CONTAINERS = [
    {
        "Id": API_ID,
        "Names": ["/muutto365-api-1"],
        "Image": "muutto365/api:latest",
        "State": "running",
        "Labels": {COMPOSE_PROJECT: "muutto365", COMPOSE_SERVICE: "api"},
    },
    {
        "Id": WORKER_ID,
        "Names": ["/muutto365-worker-1"],
        "Image": "muutto365/worker:latest",
        "State": "exited",
        "Labels": {COMPOSE_PROJECT: "muutto365", COMPOSE_SERVICE: "worker"},
    },
    {
        "Id": ORPHAN_ID,
        "Names": ["/stray"],
        "Image": "busybox:latest",
        "State": "running",
        "Labels": {},
    },
]

_INSPECT_API = {
    "Id": API_ID,
    "Name": "/muutto365-api-1",
    "Created": "2026-01-01T00:00:00.000000000Z",
    "RestartCount": 2,
    "State": {"Status": "running", "Health": {"Status": "healthy"}},
    "Config": {
        "Image": "muutto365/api:latest",
        "Labels": {COMPOSE_PROJECT: "muutto365", COMPOSE_SERVICE: "api"},
        "Tty": False,
    },
    "NetworkSettings": {
        "Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
        "Networks": {"muutto365_default": {}},
    },
}


def _multiplexed_frame(stream_id: int, text: str) -> bytes:
    payload = text.encode()
    header = bytes([stream_id]) + b"\x00\x00\x00" + struct.pack(">I", len(payload))
    return header + payload


def _driver(handler) -> DockerDriver:
    transport = httpx.MockTransport(handler)
    return DockerDriver(
        "vps1",
        client_factory=lambda: httpx.Client(
            base_url="http://docker/", transport=transport
        ),
    )


def _list_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/containers/json"
    return httpx.Response(200, json=_CONTAINERS)


def test_query_containers():
    driver = _driver(_list_handler)
    resources = driver.query(DriverResourceQuery(resource_type="container"))
    assert {r.name for r in resources} == {
        "muutto365-api-1",
        "muutto365-worker-1",
        "stray",
    }
    api = next(r for r in resources if r.name == "muutto365-api-1")
    assert api.uri == (
        "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1"
    )
    orphan = next(r for r in resources if r.name == "stray")
    assert orphan.project == "_orphans"
    assert orphan.uri == "env://vps1/docker/project/_orphans/container/stray"


def test_query_services_aggregates_by_project_and_service():
    driver = _driver(_list_handler)
    resources = driver.query(DriverResourceQuery(resource_type="service"))
    assert len(resources) == 2  # api, worker — orphans have no service
    api_service = next(r for r in resources if r.name == "api")
    assert api_service.container_count == 1
    assert api_service.uri == "env://vps1/docker/project/muutto365/service/api"


def test_query_projects_aggregates_across_services_and_orphans():
    driver = _driver(_list_handler)
    resources = driver.query(DriverResourceQuery(resource_type="project"))
    projects = {r.name: r.container_count for r in resources}
    assert projects == {"muutto365": 2, "_orphans": 1}


def test_query_filters_by_project_and_state():
    driver = _driver(_list_handler)
    resources = driver.query(
        DriverResourceQuery(
            resource_type="container", project="muutto365", state="running"
        )
    )
    assert [r.name for r in resources] == ["muutto365-api-1"]


def test_describe_container():
    def handler(request: httpx.Request) -> httpx.Response:
        # Addressed by the canonical name embedded in the URI, not the raw
        # container ID — Docker Engine accepts either, and mirarun's own
        # driver (ADR-017) addresses by name for exactly this reason.
        assert request.url.path == "/containers/muutto365-api-1/json"
        return httpx.Response(200, json=_INSPECT_API)

    driver = _driver(handler)
    details = driver.describe(
        "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1"
    )
    assert details.resource.name == "muutto365-api-1"
    assert details.health == "healthy"
    assert details.restart_count == 2
    assert details.created_at == "2026-01-01T00:00:00.000000000Z"
    assert details.ports == ("8080/tcp -> 0.0.0.0:8080",)
    assert details.networks == ("muutto365_default",)


def test_describe_service_aggregate():
    driver = _driver(_list_handler)
    details = driver.describe("env://vps1/docker/project/muutto365/service/api")
    assert details.resource.resource_type == "service"
    assert details.resource.container_count == 1


def test_describe_rejects_uri_for_a_different_target():
    driver = _driver(_list_handler)
    with pytest.raises(DriverOperationInvalid):
        driver.describe("env://other-target/docker/project/muutto365")


def test_logs_decodes_multiplexed_stream():
    payload = _multiplexed_frame(1, "hello stdout\n") + _multiplexed_frame(
        2, "oh no stderr\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/muutto365-api-1/json":
            return httpx.Response(200, json=_INSPECT_API)
        assert request.url.path == "/containers/muutto365-api-1/logs"
        return httpx.Response(200, content=payload)

    driver = _driver(handler)
    result = driver.logs(
        "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1",
        tail=100,
    )
    assert not result.truncated
    texts = [(e.stream, e.text) for e in result.entries]
    assert ("stdout", "hello stdout") in texts
    assert ("stderr", "oh no stderr") in texts


def test_logs_requires_container_address():
    driver = _driver(_list_handler)
    with pytest.raises(DriverOperationInvalid):
        driver.logs("env://vps1/docker/project/muutto365/service/api", tail=100)


def test_logs_rejects_tail_out_of_bounds():
    driver = _driver(_list_handler)
    with pytest.raises(DriverOperationInvalid):
        driver.logs(
            "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1",
            tail=0,
        )
    with pytest.raises(DriverOperationInvalid):
        driver.logs(
            "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1",
            tail=1001,
        )


@pytest.mark.parametrize("status_code", [401, 403])
def test_maps_auth_errors_to_unavailable(status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    driver = _driver(handler)
    with pytest.raises(DriverUnavailable):
        driver.query(DriverResourceQuery(resource_type="container"))


def test_maps_404_to_operation_invalid():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    driver = _driver(handler)
    with pytest.raises(DriverOperationInvalid):
        driver.query(DriverResourceQuery(resource_type="container"))


def test_404_is_also_resource_not_found():
    # The refined class must remain catchable as DriverOperationInvalid
    # (the test above) — that is the ADR-017 contract callers were written
    # against; the subclass only adds precision.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    driver = _driver(handler)
    with pytest.raises(DriverResourceNotFound):
        driver.query(DriverResourceQuery(resource_type="container"))


def test_maps_server_error_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    driver = _driver(handler)
    with pytest.raises(DriverUnavailable):
        driver.query(DriverResourceQuery(resource_type="container"))


def test_maps_network_error_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    driver = _driver(handler)
    with pytest.raises(DriverUnavailable):
        driver.query(DriverResourceQuery(resource_type="container"))


def test_rejects_malformed_container_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    driver = _driver(handler)
    with pytest.raises(DriverProtocolError):
        driver.query(DriverResourceQuery(resource_type="container"))


def test_rejects_container_missing_paired_compose_labels():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "Id": API_ID,
                    "Names": ["/half-labeled"],
                    "Image": "x",
                    "State": "running",
                    # project set, service missing — must fail closed, not
                    # silently treat as unlabelled.
                    "Labels": {COMPOSE_PROJECT: "muutto365"},
                }
            ],
        )

    driver = _driver(handler)
    with pytest.raises(DriverProtocolError):
        driver.query(DriverResourceQuery(resource_type="container"))


def test_enforces_container_inventory_cap():
    huge = [dict(_CONTAINERS[0], Id=f"{i:064x}") for i in range(10_001)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=huge)

    driver = _driver(handler)
    with pytest.raises(DriverProtocolError):
        driver.query(DriverResourceQuery(resource_type="container"))


_STATS = {
    "read": "2026-08-07T10:00:00.000000000Z",
    "cpu_stats": {
        "cpu_usage": {"total_usage": 5_000_000_000},
        "system_cpu_usage": 100_000_000_000,
        "online_cpus": 2,
    },
    "memory_stats": {"usage": 104_857_600, "limit": 536_870_912},
    "networks": {
        "eth0": {"rx_bytes": 1000, "tx_bytes": 2000},
        "eth1": {"rx_bytes": 500, "tx_bytes": 300},
    },
    "blkio_stats": {
        "io_service_bytes_recursive": [
            # cgroup v1 capitalises ops, v2 does not — both must count.
            {"op": "Read", "value": 4096},
            {"op": "read", "value": 1024},
            {"op": "Write", "value": 8192},
        ]
    },
}

API_URI = "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1"


def test_stats_one_shot_sample():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/muutto365-api-1/json":
            return httpx.Response(200, json=_INSPECT_API)
        assert request.url.path == "/containers/muutto365-api-1/stats"
        # one-shot: the daemon answers immediately instead of blocking a
        # second to pre-compute a rate we would discard anyway.
        assert request.url.params["stream"] == "false"
        assert request.url.params["one-shot"] == "true"
        return httpx.Response(200, json=_STATS)

    driver = _driver(handler)
    sample = driver.stats(API_URI)
    assert sample.uri == API_URI
    assert sample.read_at == "2026-08-07T10:00:00.000000000Z"
    assert sample.cpu_total_ns == 5_000_000_000
    assert sample.cpu_system_ns == 100_000_000_000
    assert sample.online_cpus == 2
    assert sample.memory_usage_bytes == 104_857_600
    assert sample.memory_limit_bytes == 536_870_912
    assert sample.network_rx_bytes == 1500
    assert sample.network_tx_bytes == 2300
    assert sample.block_read_bytes == 4096 + 1024
    assert sample.block_write_bytes == 8192


def test_stats_tolerates_missing_sections():
    # A stopped container answers with empty/absent sections; every field
    # must degrade to None, never to a guessed zero.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/muutto365-api-1/json":
            return httpx.Response(200, json=_INSPECT_API)
        return httpx.Response(200, json={"memory_stats": {}})

    driver = _driver(handler)
    sample = driver.stats(API_URI)
    assert sample.cpu_total_ns is None
    assert sample.memory_usage_bytes is None
    assert sample.network_rx_bytes is None
    assert sample.block_read_bytes is None


def test_stats_requires_container_address():
    driver = _driver(_list_handler)
    with pytest.raises(DriverOperationInvalid):
        driver.stats("env://vps1/docker/project/muutto365/service/api")


def test_describe_reports_lifecycle_fields():
    inspect = dict(_INSPECT_API)
    inspect["State"] = {
        "Status": "exited",
        "ExitCode": 137,
        "OOMKilled": True,
        "StartedAt": "2026-08-07T09:00:00.000000000Z",
        "FinishedAt": "2026-08-07T10:00:00.000000000Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=inspect)

    driver = _driver(handler)
    details = driver.describe(API_URI)
    assert details.exit_code == 137
    assert details.oom_killed is True
    assert details.started_at == "2026-08-07T09:00:00.000000000Z"
    assert details.finished_at == "2026-08-07T10:00:00.000000000Z"


def test_describe_normalizes_dockers_zero_value_timestamps():
    # Docker reports "never finished" as 0001-01-01T00:00:00Z, not null.
    inspect = dict(_INSPECT_API)
    inspect["State"] = {
        "Status": "running",
        "StartedAt": "2026-08-07T09:00:00.000000000Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=inspect)

    driver = _driver(handler)
    details = driver.describe(API_URI)
    assert details.finished_at is None
    assert details.started_at == "2026-08-07T09:00:00.000000000Z"


def test_client_is_reused_across_requests():
    # The deployment model is a long-running poller; a fresh client (and
    # handshake) per request was flagged as churn. One factory call, many
    # requests.
    calls = 0
    transport = httpx.MockTransport(_list_handler)

    def factory() -> httpx.Client:
        nonlocal calls
        calls += 1
        return httpx.Client(base_url="http://docker/", transport=transport)

    driver = DockerDriver("vps1", client_factory=factory)
    driver.query(DriverResourceQuery(resource_type="container"))
    driver.query(DriverResourceQuery(resource_type="container"))
    assert calls == 1
    driver.close()
    driver.query(DriverResourceQuery(resource_type="container"))
    assert calls == 2  # close() resets; next use reopens


def test_docker_driver_satisfies_environment_driver_protocol():
    # Guards the structural contract: DockerDriver never inherits from
    # EnvironmentDriver, so nothing else would catch a drifted signature.
    assert isinstance(DockerDriver("vps1"), EnvironmentDriver)
