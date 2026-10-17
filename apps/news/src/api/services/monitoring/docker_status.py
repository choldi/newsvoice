"""Read-only Docker Engine container status over the UNIX socket.

The worker container mounts ``/var/run/docker.sock`` (read-only) so the system
monitor can reflect the real state of every news-engine container (api, worker,
temporal, there, ...). The Docker REST API is called through an httpx transport
bound to the socket; no new dependency is required.
"""

from __future__ import annotations

import structlog
from httpx import AsyncClient, AsyncHTTPTransport

from src.core.config import get_settings

logger = structlog.get_logger(__name__)

_CONTAINERS_ENDPOINT = "http://docker/containers/json?all=true"


class DockerNotAvailableError(Exception):
    """Raised when the Docker socket is missing or unreachable."""


class DockerService:
    """Query the Docker Engine API through the mounted UNIX socket."""

    def __init__(self, socket_path: str | None = None) -> None:
        """Initialize the service.

        Args:
            socket_path: Path to the Docker Engine socket. Defaults to
                settings.monitor_docker_socket ("" disables the check).
        """
        settings = get_settings()
        self._socket = socket_path or settings.monitor_docker_socket

    @property
    def enabled(self) -> bool:
        """Return whether the Docker socket path is configured.

        Returns:
            True when the socket path is non-empty.
        """
        return bool(self._socket)

    async def containers(self) -> list[dict]:
        """List all containers with their id, names, image, state, and status.

        Returns:
            A list of container dicts as returned by the Docker Engine API.

        Raises:
            DockerNotAvailableError: If the socket is not configured or the
                request fails (host unreachable, permission denied, etc.).
        """
        if not self.enabled:
            raise DockerNotAvailableError("Docker socket is not configured")
        transport = AsyncHTTPTransport(uds=self._socket)
        try:
            async with AsyncClient(transport=transport, timeout=5.0) as client:
                response = await client.get(_CONTAINERS_ENDPOINT)
                response.raise_for_status()
                containers = response.json()
                return containers if isinstance(containers, list) else []
        except Exception as exc:
            logger.warning("docker_socket_unavailable", error=str(exc))
            raise DockerNotAvailableError(str(exc)) from exc

    async def unhealthy(self) -> list[dict]:
        """Return the containers currently not healthy (exited/restarting/unhealthy).

        Args:
            None.

        Returns:
            The subset of containers that should trip a monitoring warning.
        """
        containers = await self.containers()
        return [
            c
            for c in containers
            if c.get("State") != "running" or "unhealthy" in (c.get("Status") or "")
        ]
