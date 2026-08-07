from __future__ import annotations

import json

import httpx
import pytest

from mira_sdk.driver.base import (
    DriverContainerStats,
    DriverResource,
    DriverResourceDetails,
)
from mira_sdk.driver.metrics import MetricSample
from mira_sdk.driver.runner import ContainerObservation, DriverEvent, DriverSnapshot
from mira_sdk.driver.sinks import AdminCollectorSink, MirarunReportSink, SinkPublishError

API_URI = "env://vps1/docker/project/muutto365/service/api/container/muutto365-api-1"

# The exact field set mirarun's ReportedResourceRequest accepts — its model
# is extra="forbid", so an extra key here is a rejected report in production.
MIRARUN_REPORT_FIELDS = {
    "uri", "name", "project", "service", "state", "image",
    "created_at", "health", "restart_count", "ports", "networks",
}


def _observation(
    *,
    uri: str = API_URI,
    health: str | None = "healthy",
    stats: DriverContainerStats | None = None,
    cpu_percent: float | None = 12.5,
    ports: tuple[str, ...] = ("8080/tcp -> 0.0.0.0:8080",),
) -> ContainerObservation:
    return ContainerObservation(
        details=DriverResourceDetails(
            resource=DriverResource(
                uri=uri,
                resource_type="container",
                name="muutto365-api-1",
                project="muutto365",
                service="api",
                state="running",
                image="muutto365/api:latest",
            ),
            created_at="2026-01-01T00:00:00Z",
            health=health,
            restart_count=2,
            ports=ports,
            networks=("movingfirm_internal_backend",),
            exit_code=None,
            started_at="2026-08-07T09:00:00Z",
        ),
        stats=stats,
        cpu_percent=cpu_percent,
    )


def _snapshot(**kwargs) -> DriverSnapshot:
    kwargs.setdefault("target_reference", "vps1")
    kwargs.setdefault("collected_at", "2026-08-07T12:00:00+00:00")
    kwargs.setdefault("containers", (_observation(),))
    return DriverSnapshot(**kwargs)


def _capture(status_code: int = 204, body: dict | None = None):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if body is not None:
            return httpx.Response(status_code, json=body)
        return httpx.Response(status_code)

    return requests, httpx.MockTransport(handler)


def _mirarun_sink(transport: httpx.MockTransport) -> MirarunReportSink:
    return MirarunReportSink(
        report_url="http://mirarun.internal/api/target-environments/tid/report",
        token="report-credential",
        client_factory=lambda: httpx.Client(transport=transport),
    )


def test_mirarun_sink_sends_the_adr022_contract_exactly():
    requests, transport = _capture(204)
    _mirarun_sink(transport).publish(_snapshot())
    request = requests[0]
    assert request.headers["Authorization"] == "Bearer report-credential"
    assert request.headers["Content-Type"] == "application/json"
    payload = json.loads(request.content)
    assert list(payload) == ["resources"]
    resource = payload["resources"][0]
    # extra="forbid" on the server: any key outside the contract is a
    # rejected report, so the key set is the assertion — not just presence.
    assert set(resource) <= MIRARUN_REPORT_FIELDS
    assert resource["uri"] == API_URI
    assert resource["restart_count"] == 2
    assert resource["ports"] == ["8080/tcp -> 0.0.0.0:8080"]


def test_mirarun_sink_omits_none_fields_entirely():
    # The server's optional fields are min_length=1 — an explicit null or ""
    # fails validation, so absence is the only correct encoding.
    requests, transport = _capture(204)
    _mirarun_sink(transport).publish(
        _snapshot(containers=(_observation(health=None),))
    )
    resource = json.loads(requests[0].content)["resources"][0]
    assert "health" not in resource


def test_mirarun_sink_truncates_oversized_port_strings():
    long_port = "5432/tcp -> " + ", ".join(f"10.8.0.{i}:5432" for i in range(40))
    assert len(long_port) > 256
    requests, transport = _capture(204)
    _mirarun_sink(transport).publish(
        _snapshot(containers=(_observation(ports=(long_port,)),))
    )
    resource = json.loads(requests[0].content)["resources"][0]
    assert len(resource["ports"][0]) == 256


def test_mirarun_sink_raises_on_rejection():
    _, transport = _capture(422)
    with pytest.raises(SinkPublishError):
        _mirarun_sink(transport).publish(_snapshot())


def test_mirarun_sink_raises_on_unreachable_destination():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(SinkPublishError):
        _mirarun_sink(httpx.MockTransport(handler)).publish(_snapshot())


def test_mirarun_sink_refuses_to_exceed_the_resource_cap():
    observations = tuple(
        _observation(
            uri=f"env://vps1/docker/project/muutto365/service/api/container/c{i}"
        )
        for i in range(10_001)
    )
    requests, transport = _capture(204)
    with pytest.raises(SinkPublishError):
        _mirarun_sink(transport).publish(_snapshot(containers=observations))
    assert requests == []  # failed loud before sending, not after


def _admin_sink(transport: httpx.MockTransport) -> AdminCollectorSink:
    return AdminCollectorSink(
        collector_url="http://10.8.0.4:6767/api/ops/infra/collector",
        token="collector-token",
        node_id="vps1",
        host="muutto-vps",
        environment="prod",
        client_factory=lambda: httpx.Client(transport=transport),
    )


def test_admin_sink_sends_resource_metrics_containers_and_events():
    requests, transport = _capture(200, body={"status": "ok"})
    snapshot = _snapshot(
        containers=(
            _observation(
                stats=DriverContainerStats(
                    uri=API_URI,
                    memory_usage_bytes=104_857_600,
                    memory_limit_bytes=536_870_912,
                ),
            ),
        ),
        host_metrics=(
            MetricSample(
                name="system.memory.usage",
                value=1024.0,
                unit="By",
                attributes=(("state", "used"),),
            ),
        ),
        events=(
            DriverEvent(
                kind="container.oom_killed",
                uri=API_URI,
                occurred_at="2026-08-07T12:00:00+00:00",
                exit_code=137,
                log_tail=("2026-08-07T11:59:58Z [stderr] MemoryError",),
            ),
        ),
    )
    _admin_sink(transport).publish(snapshot)
    request = requests[0]
    assert request.headers["Authorization"] == "Bearer collector-token"
    payload = json.loads(request.content)
    assert payload["resource"] == {"node_id": "vps1", "host": "muutto-vps", "env": "prod"}

    metric = payload["metrics"][0]
    # Attributes flatten into the name — admin's series key is (node, name,
    # ts), so attribute-distinct series must be name-distinct.
    assert metric["name"] == "system.memory.usage{state=used}"
    assert metric["ts"] == "2026-08-07T12:00:00+00:00"

    container = payload["containers"][0]
    # The env:// URI is the join key across mirarun and admin.
    assert container["id"] == API_URI
    assert container["cpu_pct"] == 12.5
    assert container["memory_usage_bytes"] == 104_857_600

    event = payload["events"][0]
    assert event["kind"] == "container.oom_killed"
    assert event["log_tail"] == ["2026-08-07T11:59:58Z [stderr] MemoryError"]


def test_admin_sink_flattens_plain_metric_names_untouched():
    requests, transport = _capture(200, body={"status": "ok"})
    _admin_sink(transport).publish(
        _snapshot(host_metrics=(MetricSample(name="system.uptime", value=42.0, unit="s"),))
    )
    assert json.loads(requests[0].content)["metrics"][0]["name"] == "system.uptime"


def test_admin_sink_raises_on_rejection():
    _, transport = _capture(401, body={"detail": "Invalid collector token"})
    with pytest.raises(SinkPublishError):
        _admin_sink(transport).publish(_snapshot())
