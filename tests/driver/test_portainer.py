from __future__ import annotations

import httpx
import pytest

from mira_sdk.driver.base import DriverResourceQuery, DriverUnavailable, EnvironmentDriver
from mira_sdk.driver.portainer import PortainerDriver

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
API_ID = "a" * 64

_CONTAINERS = [
    {
        "Id": API_ID,
        "Names": ["/muutto365-api-1"],
        "Image": "muutto365/api:latest",
        "State": "running",
        "Labels": {COMPOSE_PROJECT: "muutto365", COMPOSE_SERVICE: "api"},
    }
]


def _proxied_driver(handler) -> PortainerDriver:
    # The factory mirrors what the driver builds itself: base_url carrying
    # the /api/endpoints/{id}/docker prefix. The handler asserts the FULL
    # proxied path, which is the thing this driver exists to get right.
    transport = httpx.MockTransport(handler)
    return PortainerDriver(
        "vps1",
        portainer_url="https://portainer.internal",
        endpoint_id=2,
        api_key="ptr_token",
        client_factory=lambda: httpx.Client(
            base_url="https://portainer.internal/api/endpoints/2/docker/",
            transport=transport,
        ),
    )


def test_requests_go_through_the_portainer_docker_proxy_path():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_CONTAINERS)

    driver = _proxied_driver(handler)
    resources = driver.query(DriverResourceQuery(resource_type="container"))
    # Portainer proxies the raw Engine API — same relative path, prefixed.
    assert seen == ["/api/endpoints/2/docker/containers/json"]
    # And the responses are the daemon's own, so identity rules (Compose
    # labels → env:// URIs) hold unchanged through the proxy.
    assert resources[0].uri == (
        "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1"
    )


def test_default_client_carries_the_api_key_and_proxied_base_url():
    driver = PortainerDriver(
        "vps1",
        portainer_url="https://portainer.internal/",  # trailing slash tolerated
        endpoint_id=2,
        api_key="ptr_token",
    )
    client = driver._client()
    try:
        assert str(client.base_url) == "https://portainer.internal/api/endpoints/2/docker/"
        assert client.headers["X-API-Key"] == "ptr_token"
    finally:
        driver.close()


def test_rejected_api_key_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    driver = _proxied_driver(handler)
    with pytest.raises(DriverUnavailable):
        driver.query(DriverResourceQuery(resource_type="container"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"portainer_url": "portainer.internal", "endpoint_id": 2, "api_key": "k"},
        {"portainer_url": "https://p.internal", "endpoint_id": 0, "api_key": "k"},
        {"portainer_url": "https://p.internal", "endpoint_id": 2, "api_key": ""},
    ],
)
def test_rejects_misconfiguration_at_construction(kwargs):
    with pytest.raises(ValueError):
        PortainerDriver("vps1", **kwargs)


def test_portainer_driver_satisfies_environment_driver_protocol():
    driver = PortainerDriver(
        "vps1", portainer_url="https://p.internal", endpoint_id=2, api_key="k"
    )
    assert isinstance(driver, EnvironmentDriver)
