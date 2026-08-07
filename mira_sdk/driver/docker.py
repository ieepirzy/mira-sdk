"""Bounded, read-only Docker Engine driver (ADR-017-equivalent, standalone).

Same bounded contract mirarun's own Docker inspector implements — list,
inspect one, tail logs; nothing else. Reimplemented here (not imported from
mirarun) because a driver process is a separate deployable that doesn't
depend on mirarun's internals. Compose project/service labels are
authoritative for identity, never container names; unlabelled containers
surface under the synthetic `_orphans` project rather than being hidden.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any

import httpx

from mira_sdk.driver.base import (
    DriverContainerStats,
    DriverLogEntry,
    DriverLogResult,
    DriverOperationInvalid,
    DriverProtocolError,
    DriverResource,
    DriverResourceDetails,
    DriverResourceNotFound,
    DriverResourceQuery,
    DriverUnavailable,
)
from mira_sdk.driver.uri import DockerUriInvalid, build_docker_uri, parse_docker_uri

_COMPOSE_PROJECT = "com.docker.compose.project"
_COMPOSE_SERVICE = "com.docker.compose.service"
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (.*)$", re.DOTALL
)
_MAX_INVENTORY_BYTES = 8 * 1024 * 1024
_MAX_CONTAINERS = 10_000
_MAX_LOG_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class _Container:
    id: str
    address_name: str
    display_name: str
    project: str
    service: str | None
    state: str | None
    image: str | None


class DockerDriver:
    """Reads one Docker Engine, producing resources addressed under
    `target_reference` — the reference mirarun's registry knows this
    endpoint by."""

    def __init__(
        self,
        target_reference: str,
        *,
        endpoint: str = "unix:///var/run/docker.sock",
        client_factory: Callable[[], httpx.Client] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._reference = target_reference
        self._endpoint = endpoint
        self._client_factory = client_factory
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client_instance: httpx.Client | None = None

    def close(self) -> None:
        """Close the underlying HTTP client. The driver remains usable — the
        next request opens a fresh client."""
        if self._client_instance is not None:
            self._client_instance.close()
            self._client_instance = None

    def __enter__(self) -> "DockerDriver":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def query(self, query: DriverResourceQuery) -> list[DriverResource]:
        containers = self._list_containers()
        if query.resource_type == "container":
            resources = [self._container_resource(c) for c in containers]
        elif query.resource_type == "service":
            resources = self._service_resources(containers)
        elif query.resource_type == "project":
            resources = self._project_resources(containers)
        else:
            raise DriverOperationInvalid("unsupported resource type")
        return [r for r in resources if _matches(r, query)]

    def describe(self, uri: str) -> DriverResourceDetails:
        try:
            address = parse_docker_uri(uri)
        except DockerUriInvalid as error:
            raise DriverOperationInvalid(str(error)) from error
        if address.target_reference != self._reference:
            raise DriverOperationInvalid(
                "address does not belong to this driver's target"
            )
        if address.container is None:
            resource_type = "service" if address.service else "project"
            matches = self.query(
                DriverResourceQuery(
                    resource_type=resource_type,
                    project=address.project,
                    service=address.service,
                    name=address.service or address.project,
                )
            )
            if len(matches) != 1:
                raise DriverOperationInvalid("target resource was not found")
            return DriverResourceDetails(resource=matches[0])

        document = self._get_json(f"containers/{address.container}/json")
        if not isinstance(document, dict):
            raise DriverProtocolError("Docker inspect response must be an object")
        container = _container_from_inspect(document)
        _assert_matches(address, container)
        state = document.get("State")
        network_settings = document.get("NetworkSettings")
        if not isinstance(state, dict):
            state = {}
        if not isinstance(network_settings, dict):
            network_settings = {}
        health_document = state.get("Health")
        health = (
            _bounded_optional(health_document.get("Status"), maximum=64, field="health")
            if isinstance(health_document, dict)
            else None
        )
        restart_count = document.get("RestartCount")
        if not isinstance(restart_count, int) or restart_count < 0:
            restart_count = None
        created_at = _bounded_optional(
            document.get("Created"), maximum=128, field="created timestamp"
        )
        exit_code = state.get("ExitCode")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            exit_code = None
        oom_killed = state.get("OOMKilled")
        if not isinstance(oom_killed, bool):
            oom_killed = None
        return DriverResourceDetails(
            resource=self._container_resource(container),
            created_at=created_at,
            health=health,
            restart_count=restart_count,
            ports=_safe_ports(network_settings.get("Ports")),
            networks=_safe_networks(network_settings.get("Networks")),
            exit_code=exit_code,
            oom_killed=oom_killed,
            started_at=_lifecycle_timestamp(state.get("StartedAt")),
            finished_at=_lifecycle_timestamp(state.get("FinishedAt")),
        )

    def logs(self, uri: str, *, tail: int) -> DriverLogResult:
        try:
            address = parse_docker_uri(uri)
        except DockerUriInvalid as error:
            raise DriverOperationInvalid(str(error)) from error
        if address.target_reference != self._reference:
            raise DriverOperationInvalid(
                "address does not belong to this driver's target"
            )
        if address.container is None:
            raise DriverOperationInvalid("Docker logs require a container address")
        if tail < 1 or tail > 1000:
            raise DriverOperationInvalid("tail must be between 1 and 1000")
        inspect = self._get_json(f"containers/{address.container}/json")
        if not isinstance(inspect, dict):
            raise DriverProtocolError("Docker inspect response must be an object")
        container = _container_from_inspect(inspect)
        _assert_matches(address, container)
        config = inspect.get("Config")
        tty = bool(config.get("Tty")) if isinstance(config, dict) else False
        payload, truncated = _read_response(
            self._client(),
            f"containers/{address.container}/logs",
            params={
                "stdout": "true",
                "stderr": "true",
                "timestamps": "true",
                "tail": str(tail),
            },
            maximum_bytes=_MAX_LOG_BYTES,
            truncate=True,
        )
        entries, frame_truncated = _decode_logs(payload, multiplexed=not tty)
        return DriverLogResult(
            entries=tuple(entries[-tail:]),
            truncated=truncated or frame_truncated or len(entries) > tail,
        )

    def stats(self, uri: str) -> DriverContainerStats:
        """One point-in-time stats sample (`one-shot=true`, so the daemon
        answers immediately instead of blocking a second to pre-compute a
        rate — rates are the caller's job, from two samples).

        Not part of `EnvironmentDriver`: the agent-facing protocol stays the
        three-operation ADR-017 surface. Stats exist for the driver runner's
        metrics collection, a trusted process on the same host."""
        try:
            address = parse_docker_uri(uri)
        except DockerUriInvalid as error:
            raise DriverOperationInvalid(str(error)) from error
        if address.target_reference != self._reference:
            raise DriverOperationInvalid(
                "address does not belong to this driver's target"
            )
        if address.container is None:
            raise DriverOperationInvalid("Docker stats require a container address")
        inspect = self._get_json(f"containers/{address.container}/json")
        if not isinstance(inspect, dict):
            raise DriverProtocolError("Docker inspect response must be an object")
        _assert_matches(address, _container_from_inspect(inspect))
        document = self._get_json(
            f"containers/{address.container}/stats",
            params={"stream": "false", "one-shot": "true"},
        )
        if not isinstance(document, dict):
            raise DriverProtocolError("Docker stats response must be an object")
        return _stats_from_document(uri, document)

    def _list_containers(self) -> list[_Container]:
        document = self._get_json("containers/json", params={"all": "true"})
        if not isinstance(document, list):
            raise DriverProtocolError("Docker container list must be an array")
        if len(document) > _MAX_CONTAINERS:
            raise DriverProtocolError("Docker container inventory exceeds limit")
        containers = []
        for item in document:
            if not isinstance(item, dict):
                raise DriverProtocolError("Docker container list contains a non-object")
            containers.append(_container_from_list(item))
        return containers

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        payload, _ = _read_response(
            self._client(),
            path,
            params=params,
            maximum_bytes=_MAX_INVENTORY_BYTES,
            truncate=False,
        )
        try:
            return httpx.Response(200, content=payload).json()
        except ValueError as error:
            raise DriverProtocolError("Docker returned malformed JSON") from error

    def _client(self) -> httpx.Client:
        # One client for the driver's lifetime, opened lazily. The stated
        # deployment model is a long-running poller; a client per request
        # re-handshakes the socket/TLS on every call for no benefit.
        # httpx.Client is safe for concurrent requests, and `close()` resets
        # cleanly if a caller wants a fresh connection pool.
        if self._client_instance is None:
            if self._client_factory is not None:
                self._client_instance = self._client_factory()
            elif self._endpoint.startswith("unix://"):
                socket_path = self._endpoint.removeprefix("unix://")
                self._client_instance = httpx.Client(
                    base_url="http://docker/",
                    transport=httpx.HTTPTransport(uds=socket_path),
                    timeout=self._timeout,
                )
            else:
                self._client_instance = httpx.Client(
                    base_url=f"{self._endpoint.rstrip('/')}/", timeout=self._timeout
                )
        return self._client_instance

    def _container_resource(self, container: _Container) -> DriverResource:
        return DriverResource(
            uri=build_docker_uri(
                self._reference,
                project=container.project,
                service=container.service,
                container=container.address_name,
            ),
            resource_type="container",
            name=container.display_name,
            project=container.project,
            service=container.service,
            state=container.state,
            image=container.image,
        )

    def _service_resources(self, containers: list[_Container]) -> list[DriverResource]:
        grouped: dict[tuple[str, str], list[_Container]] = defaultdict(list)
        for c in containers:
            if c.service is not None:
                grouped[(c.project, c.service)].append(c)
        return [
            DriverResource(
                uri=build_docker_uri(self._reference, project=project, service=service),
                resource_type="service",
                name=service,
                project=project,
                service=service,
                state=_aggregate(item.state for item in items),
                image=_aggregate(item.image for item in items),
                container_count=len(items),
            )
            for (project, service), items in grouped.items()
        ]

    def _project_resources(self, containers: list[_Container]) -> list[DriverResource]:
        grouped: dict[str, list[_Container]] = defaultdict(list)
        for c in containers:
            grouped[c.project].append(c)
        return [
            DriverResource(
                uri=build_docker_uri(self._reference, project=project),
                resource_type="project",
                name=project,
                project=project,
                state=_aggregate(item.state for item in items),
                container_count=len(items),
            )
            for project, items in grouped.items()
        ]


def _read_response(
    client: httpx.Client,
    path: str,
    *,
    params: dict[str, str] | None,
    maximum_bytes: int,
    truncate: bool,
) -> tuple[bytes, bool]:
    try:
        with client.stream("GET", path, params=params) as response:
            if response.status_code in {401, 403}:
                # Unavailable, not invalid: for the bare-socket driver there
                # is no credential to fix, and for the Portainer subclass a
                # rejected key is an operator problem either way — the
                # caller's request was fine.
                raise DriverUnavailable("target rejected the request as unauthorized")
            if response.status_code == 404:
                # DriverResourceNotFound subclasses DriverOperationInvalid,
                # so callers written against the original mapping keep
                # working while the runner can tell "vanished mid-poll" apart.
                raise DriverResourceNotFound("target resource was not found")
            if response.status_code >= 400:
                raise DriverUnavailable(
                    f"target request failed with HTTP {response.status_code}"
                )
            chunks: list[bytes] = []
            size = 0
            was_truncated = False
            for chunk in response.iter_bytes():
                remaining = maximum_bytes - size
                if len(chunk) > remaining:
                    if not truncate:
                        raise DriverProtocolError(
                            "target response exceeds the configured limit"
                        )
                    chunks.append(chunk[:remaining])
                    was_truncated = True
                    break
                chunks.append(chunk)
                size += len(chunk)
            return b"".join(chunks), was_truncated
    except httpx.RequestError as error:
        raise DriverUnavailable("target is unavailable") from error


def _container_from_list(document: dict[str, Any]) -> _Container:
    container_id = document.get("Id")
    names = document.get("Names")
    labels = document.get("Labels")
    if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
        raise DriverProtocolError("Docker container has an invalid ID")
    if (
        not isinstance(names, list)
        or len(names) > 64
        or any(not isinstance(n, str) or len(n) > 255 for n in names)
    ):
        raise DriverProtocolError("Docker container names are malformed")
    if not isinstance(labels, dict):
        raise DriverProtocolError("Docker container labels are malformed")
    clean_names = sorted(n.removeprefix("/") for n in names if n)
    raw_name = clean_names[0] if clean_names else ""
    return _container(
        container_id=container_id,
        raw_name=raw_name,
        labels=labels,
        state=_bounded_optional(document.get("State"), maximum=64, field="state"),
        image=_bounded_optional(document.get("Image"), maximum=512, field="image"),
    )


def _container_from_inspect(document: dict[str, Any]) -> _Container:
    container_id = document.get("Id")
    raw_name = document.get("Name")
    config = document.get("Config")
    state = document.get("State")
    if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
        raise DriverProtocolError("Docker container has an invalid ID")
    if not isinstance(raw_name, str) or len(raw_name) > 255:
        raise DriverProtocolError("Docker container name is malformed")
    if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
        raise DriverProtocolError("Docker container configuration is malformed")
    state_value = (
        _bounded_optional(state.get("Status"), maximum=64, field="state")
        if isinstance(state, dict)
        else None
    )
    return _container(
        container_id=container_id,
        raw_name=raw_name.removeprefix("/"),
        labels=config["Labels"],
        state=state_value,
        image=_bounded_optional(config.get("Image"), maximum=512, field="image"),
    )


def _container(
    *, container_id: str, raw_name: str, labels: dict[str, Any], state: Any, image: Any
) -> _Container:
    project_value = labels.get(_COMPOSE_PROJECT)
    service_value = labels.get(_COMPOSE_SERVICE)
    if project_value is None and service_value is None:
        project, service = "_orphans", None
    elif isinstance(project_value, str) and isinstance(service_value, str):
        project, service = project_value, service_value
        _validate_compose_address(project, service)
    else:
        raise DriverProtocolError(
            "Docker Compose project/service labels must appear together"
        )
    display_name = raw_name or container_id[:12]
    address_name = (
        display_name
        if _valid_container_address(project, service, display_name)
        else container_id[:12]
    )
    return _Container(
        id=container_id,
        address_name=address_name,
        display_name=display_name,
        project=project,
        service=service,
        state=state,
        image=image,
    )


def _validate_compose_address(project: str, service: str) -> None:
    candidate = f"env://target/docker/project/{project}/service/{service}"
    try:
        parse_docker_uri(candidate)
    except DockerUriInvalid as error:
        raise DriverProtocolError(
            "Docker Compose labels cannot form a canonical resource address"
        ) from error


def _valid_container_address(project: str, service: str | None, name: str) -> bool:
    base = f"env://target/docker/project/{project}"
    if service is not None:
        base += f"/service/{service}"
    try:
        parse_docker_uri(f"{base}/container/{name}")
    except DockerUriInvalid:
        return False
    return True


def _assert_matches(address, container: _Container) -> None:
    if (
        address.project != container.project
        or address.service != container.service
        or address.container not in {container.address_name, container.id, container.id[:12]}
    ):
        raise DriverOperationInvalid(
            "container does not belong to the addressed project and service"
        )


def _matches(resource: DriverResource, query: DriverResourceQuery) -> bool:
    return (
        (query.project is None or resource.project == query.project)
        and (query.service is None or resource.service == query.service)
        and (query.name is None or resource.name == query.name)
        and (query.state is None or resource.state == query.state)
    )


def _aggregate(values) -> str | None:
    unique = {v for v in values if v is not None}
    if not unique:
        return None
    if len(unique) == 1:
        return next(iter(unique))
    return "mixed"


def _safe_ports(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    ports = []
    for container_port, bindings in sorted(value.items()):
        if not isinstance(container_port, str) or len(container_port) > 64:
            continue
        if bindings is None:
            ports.append(container_port)
        elif isinstance(bindings, list):
            safe_bindings = []
            for binding in bindings[:16]:
                if not isinstance(binding, dict):
                    continue
                host_ip = binding.get("HostIp")
                host_port = binding.get("HostPort")
                if (
                    isinstance(host_ip, str)
                    and len(host_ip) <= 64
                    and isinstance(host_port, str)
                    and len(host_port) <= 16
                ):
                    safe_bindings.append(f"{host_ip}:{host_port}")
            ports.append(
                f"{container_port} -> {', '.join(safe_bindings)}"
                if safe_bindings
                else container_port
            )
        if len(ports) >= 128:
            break
    return tuple(ports)


def _safe_networks(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(sorted(k for k in value if isinstance(k, str) and len(k) <= 255)[:64])


def _bounded_optional(value: Any, *, maximum: int, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise DriverProtocolError(f"Docker container {field} is malformed")
    return value


def _lifecycle_timestamp(value: Any) -> str | None:
    """Docker reports "never happened" as a zero-value timestamp
    (0001-01-01T00:00:00Z), not null — normalize that to None so consumers
    don't have to know the sentinel."""
    timestamp = _bounded_optional(value, maximum=128, field="lifecycle timestamp")
    if timestamp is None or timestamp.startswith("0001-01-01"):
        return None
    return timestamp


def _stats_from_document(uri: str, document: dict[str, Any]) -> DriverContainerStats:
    cpu = document.get("cpu_stats")
    if not isinstance(cpu, dict):
        cpu = {}
    cpu_usage = cpu.get("cpu_usage")
    if not isinstance(cpu_usage, dict):
        cpu_usage = {}
    memory = document.get("memory_stats")
    if not isinstance(memory, dict):
        memory = {}
    networks = document.get("networks")
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    if isinstance(networks, dict):
        rx_values = [_safe_counter(v.get("rx_bytes")) for v in networks.values() if isinstance(v, dict)]
        tx_values = [_safe_counter(v.get("tx_bytes")) for v in networks.values() if isinstance(v, dict)]
        if rx_values and all(v is not None for v in rx_values):
            rx_bytes = sum(v for v in rx_values if v is not None)
        if tx_values and all(v is not None for v in tx_values):
            tx_bytes = sum(v for v in tx_values if v is not None)
    blkio = document.get("blkio_stats")
    block_read: int | None = None
    block_write: int | None = None
    if isinstance(blkio, dict) and isinstance(blkio.get("io_service_bytes_recursive"), list):
        read_total = 0
        write_total = 0
        saw_read = False
        saw_write = False
        for entry in blkio["io_service_bytes_recursive"]:
            if not isinstance(entry, dict):
                continue
            op = entry.get("op")
            value = _safe_counter(entry.get("value"))
            if not isinstance(op, str) or value is None:
                continue
            # cgroup v1 capitalises the op ("Read"), v2 does not ("read").
            if op.lower() == "read":
                read_total += value
                saw_read = True
            elif op.lower() == "write":
                write_total += value
                saw_write = True
        block_read = read_total if saw_read else None
        block_write = write_total if saw_write else None
    return DriverContainerStats(
        uri=uri,
        read_at=_bounded_optional(document.get("read"), maximum=128, field="stats timestamp"),
        cpu_total_ns=_safe_counter(cpu_usage.get("total_usage")),
        cpu_system_ns=_safe_counter(cpu.get("system_cpu_usage")),
        online_cpus=_safe_counter(cpu.get("online_cpus")),
        memory_usage_bytes=_safe_counter(memory.get("usage")),
        memory_limit_bytes=_safe_counter(memory.get("limit")),
        network_rx_bytes=rx_bytes,
        network_tx_bytes=tx_bytes,
        block_read_bytes=block_read,
        block_write_bytes=block_write,
    )


def _safe_counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _decode_logs(payload: bytes, *, multiplexed: bool) -> tuple[list[DriverLogEntry], bool]:
    frames: list[tuple[str, bytes]] = []
    truncated = False
    if not multiplexed:
        frames.append(("stdout", payload))
    else:
        offset = 0
        while offset < len(payload):
            if len(payload) - offset < 8:
                truncated = True
                break
            stream_id = payload[offset]
            if payload[offset + 1 : offset + 4] != b"\x00\x00\x00":
                raise DriverProtocolError("Docker log stream header is malformed")
            size = int.from_bytes(payload[offset + 4 : offset + 8], "big")
            offset += 8
            if size > _MAX_LOG_BYTES:
                raise DriverProtocolError("Docker log frame exceeds limit")
            if len(payload) - offset < size:
                truncated = True
                break
            stream = {1: "stdout", 2: "stderr"}.get(stream_id)
            if stream is None:
                raise DriverProtocolError("Docker log stream type is invalid")
            frames.append((stream, payload[offset : offset + size]))
            offset += size

    entries = []
    for stream, frame in frames:
        for raw_line in frame.decode("utf-8", errors="replace").splitlines():
            match = _TIMESTAMP.fullmatch(raw_line)
            entries.append(
                DriverLogEntry(
                    stream=stream,
                    timestamp=match.group(1) if match else None,
                    text=match.group(2) if match else raw_line,
                )
            )
    return entries, truncated
