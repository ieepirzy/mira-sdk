from __future__ import annotations

import json
import struct

import httpx
import pytest

from mira_sdk.driver.base import (
    DriverOperationInvalid,
    DriverProtocolError,
    DriverResourceQuery,
    DriverUnavailable,
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
