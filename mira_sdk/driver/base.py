"""Normalized environment driver contracts.

Mirrors the shape of mirarun's `TargetResourceInspector` (ADR-017) so a
driver's answers slot into mirarun's resource model without translation at
the mirarun end. This package owns its own types rather than importing
mirarun's — mira-sdk is a separate public package and must not depend on
mirarun's internals; the two are kept in sync by matching contract, not by
sharing code.

A driver process is bound to one underlying infrastructure endpoint (e.g.
one Docker socket) at construction and produces already-qualified `env://`
URIs carrying the target reference mirarun's registry knows it by — unlike
mirarun's own inspector, which is generic across many registered targets
and receives one as an argument per call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


RESOURCE_TYPES = ("project", "service", "container")


@dataclass(frozen=True, slots=True)
class DriverResource:
    uri: str
    resource_type: str
    name: str
    project: str | None = None
    service: str | None = None
    state: str | None = None
    image: str | None = None
    container_count: int | None = None


@dataclass(frozen=True, slots=True)
class DriverResourceDetails:
    resource: DriverResource
    created_at: str | None = None
    health: str | None = None
    restart_count: int | None = None
    ports: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    # Lifecycle fields below are a superset of what mirarun's report contract
    # (ADR-022) accepts — its request model is extra="forbid", so sinks that
    # feed mirarun must select fields explicitly rather than serialising this
    # dataclass wholesale. They exist for the driver runner's death/OOM event
    # detection, which needs to observe lifecycle transitions between polls.
    exit_code: int | None = None
    oom_killed: bool | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class DriverContainerStats:
    """One point-in-time stats sample for a container.

    CPU fields are cumulative counters, not rates — a rate requires two
    samples, and whose clock spans them is a caller decision (the runner
    diffs successive poll cycles). Any field the daemon did not supply, or
    supplied in a shape this SDK does not recognise, is None rather than a
    guess."""

    uri: str
    read_at: str | None = None
    cpu_total_ns: int | None = None
    cpu_system_ns: int | None = None
    online_cpus: int | None = None
    memory_usage_bytes: int | None = None
    memory_limit_bytes: int | None = None
    network_rx_bytes: int | None = None
    network_tx_bytes: int | None = None
    block_read_bytes: int | None = None
    block_write_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class DriverLogEntry:
    stream: str
    text: str
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class DriverLogResult:
    entries: tuple[DriverLogEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class DriverResourceQuery:
    resource_type: str
    project: str | None = None
    service: str | None = None
    name: str | None = None
    state: str | None = None


class DriverError(Exception):
    """Base for normalized driver errors."""


class DriverUnavailable(DriverError):
    """The underlying infrastructure could not be reached or authenticated."""


class DriverProtocolError(DriverError):
    """The underlying infrastructure returned a malformed or unsafe response."""


class DriverOperationInvalid(DriverError):
    """A request is malformed or outside the bounded read surface."""


class DriverResourceNotFound(DriverOperationInvalid):
    """The addressed resource does not exist.

    Subclasses `DriverOperationInvalid` deliberately: mirarun's inspector
    (ADR-017) buckets a missing resource as an invalid operation, and callers
    written against that contract must keep working — this class only adds
    precision for callers that care about the distinction (the runner uses it
    to tell "container vanished mid-poll" from a genuinely malformed
    request)."""


@runtime_checkable
class EnvironmentDriver(Protocol):
    """Bounded read-only contract a driver implements for one target.

    Same three-operation shape ADR-017 established for exactly the same
    reason: a wider surface (arbitrary paths, raw inspect documents, a
    command runner) turns an agent-facing query surface into a daemon-admin
    interface.
    """

    def query(self, query: DriverResourceQuery) -> list[DriverResource]: ...

    def describe(self, uri: str) -> DriverResourceDetails: ...

    def logs(self, uri: str, *, tail: int) -> DriverLogResult: ...
