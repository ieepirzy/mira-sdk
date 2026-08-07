# mira-sdk

[![CI](https://github.com/ieepirzy/mira-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/ieepirzy/mira-sdk/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mira-sdk)](https://pypi.org/project/miraasdk/)
[![Python versions](https://img.shields.io/pypi/pyversions/mira-sdk)](https://pypi.org/project/miraasdk/)

Client SDK for agent processes in the Mira ecosystem (MiraRun/MiraGen). Ships two deliberately separated faces (see `docs/architecture.md` for why they must never share a process):

- **`mira_sdk.telemetry`** — run-scoped OTLP telemetry export, correlated with the run/routine/agent identity a Mira-orchestrated process already has. For embedding in any ordinary service.
- **`mira_sdk.driver`** — a standalone, privileged, read-only infrastructure observer: bounded Docker discovery (list/inspect/logs/stats), host metrics with OpenTelemetry semantic-convention names, poll-based container death/OOM detection with post-mortem log tails, and a report loop (`mira-driver`) that publishes to MiraRun's reported-target endpoint (ADR-022) and/or movingfirm-admin's infra collector.

## Install

```bash
pip install mira-sdk            # telemetry only
pip install 'mira-sdk[driver]'  # + the driver toolkit (httpx)
```

## Quickstart

```python
from mira_sdk import MiraTelemetry

telemetry = MiraTelemetry(
    endpoint="https://mira.example/otlp/v1/traces",
    run_id=run_id,
    routine_id=routine_id,
    routine_revision_id=routine_revision_id,
    agent_id=agent_id,
    token=run_credential,  # same run-scoped credential used for MCP auth
)

with telemetry.span("resource.query", **{"mira.resource.uri": target_uri}):
    ...  # do the thing; exceptions are recorded on the span and re-raised

telemetry.shutdown()  # once, at process exit
```

## The driver as a process

```bash
MIRA_DRIVER_TARGET_REFERENCE=vps1 \
MIRA_DRIVER_ADMIN_COLLECTOR_URL=http://10.8.0.4:6767/api/ops/infra/collector \
MIRA_DRIVER_ADMIN_COLLECTOR_TOKEN=... \
mira-driver
```

Configuration is env vars only, documented in `mira_sdk/driver/process.py`. The reference deployment — a read-only Docker socket proxy plus the driver, as one Compose stack — lives in `deploy/`; the socket proxy is the single privileged component, and it doubles as MiraRun's direct-pull endpoint on WireGuard-reachable hosts. As a library, the same pieces compose explicitly: `DriverRunner(DockerDriver(...), [MirarunReportSink(...), AdminCollectorSink(...)], ...)`.

## Design

- **Non-blocking by construction.** Spans are queued and exported on a background thread (`BatchSpanProcessor`); this module never wires a synchronous exporter. A telemetry backend being down must not fail or stall the operation that emitted the span.
- **Identity set once.** `run_id`/`agent_id`/`routine_id`/`routine_revision_id`/`service.*`/`deployment.environment` are OTel *resource* attributes — set once at construction, present on every span this instance emits. Attaching them per-span instead is the mistake that breaks filtering and aggregation once there's a real backend behind the endpoint.
- **OTLP, not a vendor SDK.** `endpoint` is a plain OTLP/HTTP traces endpoint. This package has no opinion on what's behind it — an OTel Collector, a vendor backend, or a bare receiver during development.
- **`export_stats` for observability, not a promise of completeness.** It counts export batches that reached the exporter and failed — the actionable half of "did my telemetry get out." Queue-overflow drops (a producer outpacing the export thread) are logged by OpenTelemetry's own `opentelemetry.sdk.trace.export` logger, not duplicated here.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Releasing

Publishing to PyPI happens on GitHub Release (Trusted Publishing / OIDC — no token stored in this repo). Bump `version` in `pyproject.toml`, merge, then cut a GitHub Release; `.github/workflows/release.yml` builds and publishes.
