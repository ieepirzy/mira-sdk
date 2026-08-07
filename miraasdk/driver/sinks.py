"""Snapshot sinks: where a poll cycle's results go.

Two wire contracts live here, both owned by other repos and matched by
shape, not by shared code (same policy as `base.py` vs mirarun's
inspector):

- `MirarunReportSink` → mirarun `POST /api/target-environments/{id}/report`
  (ADR-022). The request model there is `extra="forbid"` with `min_length=1`
  on optional strings, so this sink sends exactly the allowed fields and
  omits Nones entirely. Only container-address resources are sent — the
  server rejects project/service aggregates.

- `AdminCollectorSink` → movingfirm-admin `POST /api/ops/infra/collector`
  (issue #58). Admin's models ignore unknown fields, so this sink already
  sends the richer container fields and the `events` list that admin only
  *stores* from its Phase-2 migration onward — the two repos can deploy
  independently in either order.

Both sinks fail loud (`SinkPublishError`) rather than silently truncating:
a bounded-but-partial inventory report reads as authoritative absence on
the receiving end, which is worse than no report.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

import httpx

from miraasdk.driver.runner import DriverSnapshot

# Mirror of mirarun's server-side caps (domain/target_reports.py). Enforced
# client-side so an oversized inventory fails as *our* error with a clear
# message instead of an opaque 413/422.
_MAX_REPORT_RESOURCES = 10_000
_MAX_REPORT_BODY_BYTES = 8 * 1024 * 1024
_MAX_REPORTED_PORT_CHARS = 256


class SinkPublishError(Exception):
    """A sink could not deliver a snapshot to its destination."""


class _HttpSink:
    """Shared lazily-opened persistent client, same lifecycle pattern as
    `DockerDriver` and for the same reason: this runs inside a long-lived
    poller."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.Client] | None,
        timeout_seconds: float,
    ) -> None:
        self._client_factory = client_factory
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client_instance: httpx.Client | None = None

    def close(self) -> None:
        if self._client_instance is not None:
            self._client_instance.close()
            self._client_instance = None

    def _client(self) -> httpx.Client:
        if self._client_instance is None:
            if self._client_factory is not None:
                self._client_instance = self._client_factory()
            else:
                self._client_instance = httpx.Client(timeout=self._timeout)
        return self._client_instance

    def _post_json(self, url: str, token: str, payload: dict[str, Any]) -> httpx.Response:
        body = json.dumps(payload).encode()
        try:
            return self._client().post(
                url,
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.RequestError as error:
            raise SinkPublishError(f"destination is unreachable: {url}") from error


class MirarunReportSink(_HttpSink):
    def __init__(
        self,
        *,
        report_url: str,
        token: str,
        client_factory: Callable[[], httpx.Client] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(client_factory=client_factory, timeout_seconds=timeout_seconds)
        self._url = report_url
        self._token = token

    def publish(self, snapshot: DriverSnapshot) -> None:
        resources = [
            _reported_resource(observation)
            for observation in snapshot.containers
            if observation.details.resource.resource_type == "container"
        ]
        if len(resources) > _MAX_REPORT_RESOURCES:
            raise SinkPublishError(
                "inventory exceeds mirarun's per-report resource cap"
            )
        payload = {"resources": resources}
        if len(json.dumps(payload).encode()) > _MAX_REPORT_BODY_BYTES:
            raise SinkPublishError("report body exceeds mirarun's size cap")
        response = self._post_json(self._url, self._token, payload)
        if response.status_code != 204:
            raise SinkPublishError(
                f"mirarun rejected the report: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )


def _reported_resource(observation: Any) -> dict[str, Any]:
    details = observation.details
    resource = details.resource
    payload: dict[str, Any] = {
        "uri": resource.uri,
        "name": resource.name,
        "project": resource.project,
        # Driver-side port strings can exceed the server's 256-char field cap
        # when a port has many host bindings; truncated rendering beats a
        # rejected report.
        "ports": [port[:_MAX_REPORTED_PORT_CHARS] for port in details.ports],
        "networks": list(details.networks),
    }
    for field, value in (
        ("service", resource.service),
        ("state", resource.state),
        ("image", resource.image),
        ("created_at", details.created_at),
        ("health", details.health),
        ("restart_count", details.restart_count),
    ):
        if value is not None:
            payload[field] = value
    return payload


class AdminCollectorSink(_HttpSink):
    def __init__(
        self,
        *,
        collector_url: str,
        token: str,
        node_id: str,
        host: str | None = None,
        environment: str | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(client_factory=client_factory, timeout_seconds=timeout_seconds)
        self._url = collector_url
        self._token = token
        self._node_id = node_id
        self._host = host
        self._environment = environment

    def publish(self, snapshot: DriverSnapshot) -> None:
        payload = {
            "resource": {
                "node_id": self._node_id,
                "host": self._host,
                "env": self._environment,
            },
            "metrics": [
                {
                    "name": _flatten_metric_name(sample.name, sample.attributes),
                    "value": sample.value,
                    "unit": sample.unit,
                    "ts": snapshot.collected_at,
                }
                for sample in snapshot.host_metrics
            ],
            "containers": [
                _collector_container(observation)
                for observation in snapshot.containers
                if observation.details.resource.resource_type == "container"
            ],
            "events": [
                {
                    "kind": event.kind,
                    "uri": event.uri,
                    "occurred_at": event.occurred_at,
                    "exit_code": event.exit_code,
                    "log_tail": list(event.log_tail),
                }
                for event in snapshot.events
            ],
        }
        response = self._post_json(self._url, self._token, payload)
        if response.status_code < 200 or response.status_code >= 300:
            raise SinkPublishError(
                f"admin collector rejected the push: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )


def _flatten_metric_name(name: str, attributes: tuple[tuple[str, str], ...]) -> str:
    """Admin's metric rows have a flat name and no attribute slot, and the
    row key is (node, name, ts) — attributes must land in the name or the
    series collide. `system.memory.usage{state=used}` keeps the semconv name
    recoverable by splitting on the brace."""
    if not attributes:
        return name
    rendered = ",".join(f"{key}={value}" for key, value in sorted(attributes))
    return f"{name}{{{rendered}}}"


def _collector_container(observation: Any) -> dict[str, Any]:
    details = observation.details
    resource = details.resource
    return {
        # The env:// URI is the one identifier stable across the driver,
        # mirarun, and admin — using it as admin's container id makes rows
        # joinable across all three systems.
        "id": resource.uri,
        "name": resource.name,
        "state": resource.state,
        "cpu_pct": observation.cpu_percent,
        # Fields below are ignored by admin until its Phase-2 migration.
        "project": resource.project,
        "service": resource.service,
        "image": resource.image,
        "health": details.health,
        "restart_count": details.restart_count,
        "exit_code": details.exit_code,
        "started_at": details.started_at,
        "memory_usage_bytes": (
            observation.stats.memory_usage_bytes if observation.stats else None
        ),
        "memory_limit_bytes": (
            observation.stats.memory_limit_bytes if observation.stats else None
        ),
    }
