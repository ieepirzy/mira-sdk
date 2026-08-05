# mira-sdk: two faces

**Status:** design, partially implemented
**Date:** 2026-08-05

## The split

`mira-sdk` is two things that share a resource model but must never share a
process:

1. **`mira_sdk.telemetry`** — embeds in *any* Mira-orchestrated service.
   Emits OTLP spans (built, see `mira_sdk/telemetry.py`). It is passive:
   the host process calls into it, it never calls back into the host, and
   it never blocks the host on a telemetry backend being unreachable.

2. **`mira_sdk.driver`** — runs as its *own* process, deployed separately
   from anything using `telemetry`. It discovers and reads infrastructure
   (Docker/Portainer today, Kubernetes later) and reports normalized
   results back to mirarun's central MCP. This is the "Active environment
   agent" / "Compatibility adapter" split from the original environment-
   integration design note, actually pulled out as a standalone process
   rather than living inside mirarun's own request path the way Docker
   inspection does today (ADR-016/017 in `mirarun`).

Why they can't be the same face: a driver inspecting Docker/Kubernetes
needs privileged access to that infrastructure (a socket mount, a scoped
ServiceAccount) and a process lifecycle suited to a long-running poller/
watcher. A service embedding `telemetry` is ordinary application code that
happens to export spans — coupling it to driver privileges would make
"add tracing to my service" also mean "grant this service infra read
access," which is a blast-radius mistake, not a convenience.

## What ties them together

Both faces terminate at the same place: one agent, one MCP connection,
`env.list` → pick a target → `resource.query`/`resource.describe`/
`logs.query`. That surface already exists (`mirarun` ADR-021) for
infrastructure targets registered by an operator. Two things extend it:

### Driver-reported targets

A `mira_sdk.driver` process discovers targets mirarun's own process can't
reach directly (a Portainer host behind its own network boundary, or a
Kubernetes cluster) and reports them to mirarun's central MCP, which
projects them into the same `TargetEnvironment`/`env://` model ADR-016
already defines. The driver is a source of registry entries and query
answers; it does not gain its own authority — capability grants still live
only in mirarun's registry, evaluated the same way (ADR-018) regardless of
whether the query is answered by mirarun's own Docker inspector or proxied
to a driver process.

### Self-registered service targets

A process embedding `mira_sdk.telemetry` should be listable too — not as
infrastructure, but as itself: "the checkout service, read-only, here's
what's currently true about it." An agent that selects it gets the same
bounded `resource.describe`/`logs.query` shape, scoped to whatever that
service chooses to expose (health, recent errors, config summary — never
an open door). This is explicitly **read visibility, not management**:
the same v0 boundary ADR-016 already drew for infrastructure ("exactly
`["read"]`, mechanically enforced") applies here too, for the same reason
— an agent should not gain the ability to reach into arbitrary running
services and change them just because they happen to emit telemetry.

## Open design question (mirarun-side, not decided here)

How does mirarun *learn* a self-registered or driver-reported target
exists, and how does it *reach* it to answer a query?

- **Push (heartbeat/registration):** the process (driver, or a telemetry-
  embedding service) calls a mirarun registration endpoint periodically,
  carrying whatever it wants exposed. mirarun answers `resource.query`
  from its own last-known state — no live round-trip per agent query, but
  state can be stale between heartbeats.
- **Pull (proxy):** mirarun's central MCP forwards the query live to the
  registered process's own endpoint at call time. Fresher, but couples
  query latency/availability to that process being reachable and online
  exactly when an agent asks, and needs its own auth story (mirarun
  calling out to an arbitrary registered endpoint).
- **Hybrid:** push a heartbeat + capability manifest (what this target can
  answer), pull the actual query content on demand.

This needs a real mirarun-side ADR — it changes mirarun's registry from
"operator creates static rows" to "rows can also arrive dynamically from
things that aren't mirarun's own process," which is a bigger trust-boundary
question than ADR-021's credential mechanism was. Not resolved here.

## What's implemented vs. scaffolded

- `mira_sdk.telemetry` — implemented, published.
- `mira_sdk.driver.base` — normalized contracts (`DriverResource`,
  `DriverTarget`, the `EnvironmentDriver` protocol), mirroring the shape
  of mirarun's `TargetResourceInspector` (ADR-017) so a driver's answers
  slot into the same model without translation at the mirarun end.
- `mira_sdk.driver.docker` — implemented: Compose-label discovery,
  bounded list/inspect/logs, orphan handling — the same bounded read-only
  contract ADR-017 established, reimplemented here because a driver
  process needs it standalone (not a mirarun-internal call).
- `mira_sdk.driver.kubernetes` — not implemented. Nothing in the Mira
  ecosystem implements Kubernetes discovery anywhere yet; scaffolding it
  blind without a cluster to validate against would just be guessing.
- Phone-home transport (driver → mirarun, and self-registration) — not
  implemented; blocked on the open design question above.
