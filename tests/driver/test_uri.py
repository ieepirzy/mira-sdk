from __future__ import annotations

import pytest

from miraasdk.driver.uri import DockerUriInvalid, build_docker_uri, parse_docker_uri


def test_round_trips_project_only():
    uri = build_docker_uri("vps1", project="muutto365")
    assert uri == "env://vps1/docker/project/muutto365"
    address = parse_docker_uri(uri)
    assert address.target_reference == "vps1"
    assert address.project == "muutto365"
    assert address.service is None
    assert address.container is None


def test_round_trips_service():
    uri = build_docker_uri("vps1", project="muutto365", service="api")
    assert uri == "env://vps1/docker/project/muutto365/service/api"
    address = parse_docker_uri(uri)
    assert address.service == "api"


def test_round_trips_container_under_service():
    uri = build_docker_uri(
        "vps1", project="muutto365", service="api", container="muutto365-api-1"
    )
    assert uri == "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1"
    address = parse_docker_uri(uri)
    assert address.container == "muutto365-api-1"


def test_orphan_container():
    uri = build_docker_uri("vps1", project="_orphans", container="stray-container")
    assert uri == "env://vps1/docker/project/_orphans/container/stray-container"
    address = parse_docker_uri(uri)
    assert address.project == "_orphans"
    assert address.container == "stray-container"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uri",
        "http://vps1/docker/project/x",
        "env://VPS1/docker/project/x",  # uppercase reference
        "env://vps1/docker/project/",  # trailing slash / empty project
        "env://vps1/docker/project/x/",
        "env://vps1/docker/service/x",  # wrong hierarchy
        "env://vps1/k8s/ns/x",  # not docker
        "env://vps1/docker/project/x/service",  # missing service name
        "env://vps1/docker/project/x/service/y/container",  # missing container name
        "env://vps1/docker/project/x?query=1",
        "env://vps1/docker/project/x#frag",
        "env://user@vps1/docker/project/x",
        "env://vps1/docker/project/_orphans/service/y",  # orphans can't have a service
        "env://" + "a" * 64 + "/docker/project/x",  # reference too long
    ],
)
def test_rejects_invalid_uris(value):
    with pytest.raises(DockerUriInvalid):
        parse_docker_uri(value)


def test_rejects_non_canonical_percent_encoding():
    with pytest.raises(DockerUriInvalid):
        parse_docker_uri("env://vps1/docker/project/x%20y")
