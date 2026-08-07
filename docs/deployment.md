# Deploying the mira-driver stack to production

**Scope:** the reference deployment in `deploy/` — one read-only Docker
socket proxy plus the `mira-driver` process — registered as a Portainer
git-backed stack on the movingfirm VPS (`vps1`, WireGuard `10.8.0.4`),
reporting into movingfirm-admin's infra collector. A second host later is
the same runbook with a different `MIRA_DRIVER_TARGET_REFERENCE` and
WireGuard bind address.

Verified before this runbook was written (2026-08-07, without a Docker
daemon in the verifying environment, so no image build — that happens on
the VPS at deploy time): the full test suite passes on the pinned branch;
`docker compose config` renders `deploy/compose.yaml` with the exact
variable set below; and `DriverProcessConfig.from_environ` + `build_runner`
accept the rendered environment verbatim — admin sink on, mirarun sink
correctly off when its pair is left empty. The receiving endpoint
(`POST /api/ops/infra/collector`) and its `INFRA_COLLECTOR_TOKEN`
declaration exist in movingfirm-admin's `main.py` / `docker-compose.yml`
(their issue #58).

## Why a Portainer git-backed stack

One repo per stack is the house GitOps rule (the same one that split
movingfirm-admin out of the backend stack, 2026-03). Portainer clones this
repo and runs Compose against `deploy/compose.yaml`; the build context is
the repo root (see `deploy/Dockerfile`'s header), so **the running driver
is always the code on the branch Portainer pulled** — never a
possibly-older PyPI release of `miraasdk`. Redeploys are "Pull and
redeploy" in the stack view (or the stack webhook), not SSH.

## Prerequisites (one-time, before registering the stack)

1. **Mint the collector token.** `openssl rand -hex 32`. This is a
   machine-to-machine credential, deliberately separate from
   `ADMIN_SECRET_TOKEN` (a leaked dashboard session must not become a
   fleet write path — movingfirm-admin issue #58).
2. **Give the admin stack its half.** In Portainer, edit the
   movingfirm-admin stack's environment: set `INFRA_COLLECTOR_TOKEN` to
   the minted value and redeploy. The variable has been *declared* in that
   repo's `docker-compose.yml` since their issue #57/#58 cleanup, so
   setting it in Portainer is sufficient — but confirm the declaration is
   present in the deployed revision, because an undeclared variable set in
   Portainer's UI silently never reaches the container.
3. **Repository access.** If this repo is private, Portainer needs a
   read-only credential (fine-grained PAT scoped to this one repo,
   Contents: read) entered in the "Repository" authentication fields when
   registering the stack. A public repo needs nothing.

## Registering the stack

Portainer → Stacks → Add stack → **Repository**:

| Field | Value |
|---|---|
| Name | `mira-driver` |
| Repository URL | `https://github.com/ieepirzy/mira-sdk` |
| Repository reference | `refs/heads/main` |
| Compose path | `deploy/compose.yaml` |

Environment variables (every one of these is explicitly declared in
`deploy/compose.yaml` — Compose only passes through declared variables, so
adding a *new* one later means declaring it there too, or it silently
never reaches the container):

| Variable | Value for vps1 | Notes |
|---|---|---|
| `MIRA_PROXY_WG_BIND` | `10.8.0.4` | This host's WireGuard address. The socket proxy binds `2375` here **only** — never the public interface. Required; the stack refuses to start without it. |
| `MIRA_DRIVER_TARGET_REFERENCE` | `vps1` | The reference mirarun's registry knows this host by (ADR-016). Lowercase alphanumerics and hyphens. Required. |
| `MIRA_DRIVER_ADMIN_COLLECTOR_URL` | `http://10.8.0.4:6767/api/ops/infra/collector` | The admin API's WireGuard bind on this same host. |
| `MIRA_DRIVER_ADMIN_COLLECTOR_TOKEN` | the minted token | Must equal the admin stack's `INFRA_COLLECTOR_TOKEN`. |
| `MIRA_DRIVER_NODE_ENV` | `prod` | Default; set explicitly anyway so the admin dashboard label is deliberate. |
| `MIRA_DRIVER_INTERVAL_SECONDS` | `60` | Default. Minimum 5; the process refuses lower. |

Leave `MIRA_DRIVER_MIRARUN_REPORT_URL` / `_TOKEN` unset: vps1 is
WireGuard-reachable, so mirarun pulls through the socket proxy instead of
receiving pushed reports (the "reported" reachability path is for hosts
mirarun cannot reach). An empty string is treated as unset by
`process.py`, so the compose defaults are safe.

Then **Deploy the stack**. Portainer builds the driver image from the
cloned checkout on first deploy; expect a couple of minutes.

Optionally enable GitOps polling on the stack so a merge to `main`
redeploys automatically; otherwise "Pull and redeploy" manually after
merges that touch `mira_sdk/` or `deploy/`.

## Verifying the deployment

1. **Driver startup line.** Portainer → the `mira-driver` stack → `driver`
   container → Logs. A correctly configured process logs exactly one
   summary at startup:

       mira-driver polling vps1 every 60s (sinks: mirarun=off admin=on host_metrics=on)

   A misconfigured one exits with status 2 and a `mira-driver: <what is
   wrong>` line instead — configuration errors fail at startup by design,
   not silently at the first push.
2. **Data lands in admin.** The admin dashboard's Infrastructure view
   should show node `vps1` with host metrics and this host's containers
   within one interval (60 s).
3. **The proxy really is read-only.** From the VPS (or any WireGuard
   peer): `curl -s -o /dev/null -w '%{http_code}' -X POST
   http://10.8.0.4:2375/v1.44/containers/prune` → expect `403`
   (`POST: 0`), while `curl -s http://10.8.0.4:2375/v1.44/containers/json`
   returns the container list. If the second command works from a machine
   *outside* the WireGuard mesh, stop and fix the bind before anything
   else — that endpoint must never face the public interface.

## Failure modes seen before, and what they mean

- **Driver logs `401` on every push** — token mismatch, or the admin
  container never received `INFRA_COLLECTOR_TOKEN` (see prerequisite 2;
  undeclared-variable passthrough is exactly how movingfirm-admin's
  `PLAUSIBLE_*` vars were silently dropped in their issue #57).
- **Driver logs connection refused/timeout to `10.8.0.4:6767`** — the
  admin stack isn't up, or its port publish moved off the WireGuard bind.
  Container-to-host traffic to a Docker-published port does not normally
  traverse UFW's default-deny input chain, but if a firewall change lands
  on the box, this is the first symptom to re-check.
- **Stack deploy fails with a build error referencing `mira_sdk/`** — the
  compose path was registered without the repo-root build context reaching
  Portainer's clone (compose resolves `context: ..` relative to
  `deploy/`), or an in-flight rename changed the package directory. The
  fix is in the repo, not in Portainer settings: `deploy/Dockerfile` must
  `COPY` whatever the package directory is actually called on the pinned
  branch (PR #3 renames `mira_sdk` → `miraasdk`; its merge must update
  `deploy/Dockerfile` in the same commit).

## What this deliberately does not do

- **No secrets in this repo.** The collector token exists in exactly two
  places, both Portainer stack environments (admin's and this stack's).
  `deploy/.env.example` documents shape, never values.
- **No mutating Docker access anywhere.** The raw socket is mounted
  read-only into the proxy and the proxy denies every mutating verb
  (`POST: 0`). The driver cannot restart, create, or delete anything —
  it observes. Deployment *actions* (the "agent updates env vars /
  redeploys a stack" ambition) are a different privilege tier and belong
  to a separate, explicitly-authorized component with its own credential —
  never grant them to this stack by widening the proxy's permissions.
- **No public listener.** The only published port (`2375`) binds to the
  WireGuard address. Note the trade-off: every WireGuard peer on the mesh
  can read container lists, logs, and stats from this host through it.
  That is the intended contract for mirarun's pull path; if the mesh ever
  grows untrusted peers, this endpoint needs auth in front of it.
