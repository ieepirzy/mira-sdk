"""Bounded, read-only driver through a Portainer instance's Docker proxy.

Portainer exposes each managed Docker environment's raw Engine API at
`/api/endpoints/{id}/docker/*`, authenticated with an `X-API-Key` header —
the responses are the daemon's own, byte-for-byte. That makes this driver a
thin subclass of `DockerDriver`: same requests, same parsing, same caps,
same fail-closed identity rules; only the base URL and the credential
differ. Deliberately *not* the Portainer-native resource API (`/api/stacks`
etc.) — stack naming on the real deployment is inconsistent, which is
exactly why identity comes from Compose labels (mirarun ADR-016), and the
Docker proxy carries those labels unmodified.

The API key should be a Portainer access token scoped to a least-privileged
read-only user — the driver only ever issues GETs, but the credential's
blast radius is decided in Portainer, not here.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from miraasdk.driver.docker import DockerDriver


class PortainerDriver(DockerDriver):
    def __init__(
        self,
        target_reference: str,
        *,
        portainer_url: str,
        endpoint_id: int,
        api_key: str,
        client_factory: Callable[[], httpx.Client] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not portainer_url.startswith(("http://", "https://")):
            raise ValueError("portainer_url must be an http(s) URL")
        if endpoint_id < 1:
            raise ValueError("endpoint_id must be a positive Portainer endpoint id")
        if not api_key:
            raise ValueError("api_key must not be empty")
        super().__init__(
            target_reference,
            endpoint=f"{portainer_url.rstrip('/')}/api/endpoints/{endpoint_id}/docker",
            client_factory=client_factory,
            timeout_seconds=timeout_seconds,
        )
        self._api_key = api_key

    def _client(self) -> httpx.Client:
        # Same lazily-opened persistent client as the parent; the only
        # difference is the credential header. A client_factory still wins
        # outright (the test seam must see exactly what it injected).
        if self._client_instance is None and self._client_factory is None:
            self._client_instance = httpx.Client(
                base_url=f"{self._endpoint}/",
                headers={"X-API-Key": self._api_key},
                timeout=self._timeout,
            )
        return super()._client()
