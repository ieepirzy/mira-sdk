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
from typing import Protocol


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


class DriverResourceNotFound(DriverError):
    """The addressed resource does not exist."""


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
