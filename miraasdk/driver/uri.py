"""Docker-shaped subset of mirarun's `env://` URI grammar (ADR-016).

A driver process needs to parse the URIs it's asked to `describe`/`logs`
without importing mirarun's parser — this is that grammar, ported and
trimmed to the Docker hierarchy this driver actually answers for. Kept
byte-for-byte compatible with mirarun's canonicalization (same segment
validation, same `_orphans` handling) so a URI this module builds parses
identically on the mirarun side.

The Kubernetes-shaped hierarchy is intentionally not reimplemented here —
nothing in miraasdk answers for it yet (see `docs/architecture.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

_REFERENCE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SEGMENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")


class DockerUriInvalid(Exception):
    """A value is not a canonical Docker-shaped `env://` URI."""


@dataclass(frozen=True, slots=True)
class DockerAddress:
    target_reference: str
    project: str
    service: str | None = None
    container: str | None = None

    @property
    def uri(self) -> str:
        segments = ["docker", "project", self.project]
        if self.service is not None:
            segments += ["service", self.service]
        if self.container is not None:
            segments += ["container", self.container]
        return f"env://{self.target_reference}/{'/'.join(segments)}"


def build_docker_uri(
    target_reference: str,
    *,
    project: str,
    service: str | None = None,
    container: str | None = None,
) -> str:
    return DockerAddress(
        target_reference=target_reference,
        project=project,
        service=service,
        container=container,
    ).uri


def parse_docker_uri(value: str) -> DockerAddress:
    if (
        not value
        or len(value) > 1024
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "%" in value
    ):
        raise DockerUriInvalid("address is not a canonical env URI")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as error:
        raise DockerUriInvalid("address is not a canonical env URI") from error
    if (
        parsed.scheme != "env"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != parsed.hostname
        or not _valid_reference(parsed.hostname)
        or not parsed.path.startswith("/")
        or parsed.path.endswith("/")
    ):
        raise DockerUriInvalid("address is not a canonical env URI")

    segments = parsed.path[1:].split("/")
    if any(
        segment != "_orphans" and not _SEGMENT.fullmatch(segment)
        for segment in segments
    ):
        raise DockerUriInvalid("address contains an invalid path segment")
    if len(segments) < 3 or segments[:2] != ["docker", "project"]:
        raise DockerUriInvalid(
            "docker address must start with /docker/project/{project}"
        )

    reference = parsed.hostname
    project = segments[2]
    if len(segments) == 3:
        address = DockerAddress(reference, project)
    elif project == "_orphans":
        if len(segments) != 5 or segments[3] != "container":
            raise DockerUriInvalid(
                "orphan address must end with /container/{container}"
            )
        address = DockerAddress(reference, project, container=segments[4])
    elif len(segments) >= 5 and segments[3] == "service":
        service = segments[4]
        if len(segments) == 5:
            address = DockerAddress(reference, project, service=service)
        elif len(segments) == 7 and segments[5] == "container":
            address = DockerAddress(
                reference, project, service=service, container=segments[6]
            )
        else:
            raise DockerUriInvalid(
                "docker service child must be /container/{container}"
            )
    else:
        raise DockerUriInvalid("docker project child must be /service/{service}")

    if address.uri != value:
        raise DockerUriInvalid("address is not in canonical form")
    return address


def valid_target_reference(value: str) -> bool:
    """Whether a string is usable as an `env://` target reference. Public so
    process configuration can validate a reference without building and
    parsing a throwaway URI."""
    return _valid_reference(value)


def _valid_reference(value: str) -> bool:
    return len(value) <= 63 and _REFERENCE.fullmatch(value) is not None
