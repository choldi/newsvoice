"""Unit tests for DockerService (read-only socket status)."""

import pytest

from src.api.services.monitoring.docker_status import DockerNotAvailableError, DockerService
from src.core.config import settings

_ROUTE = "http://docker/containers/json?all=true"

_ROUTES: dict[str, list[dict]] = {
    _ROUTE: [
        {
            "Id": "abc",
            "Names": ["/news_engine_api"],
            "Image": "api",
            "State": "running",
            "Status": "Up 2h",
        },
        {
            "Id": "def",
            "Names": ["/news_engine_temporal"],
            "Image": "temporal",
            "State": "exited",
            "Status": "Exited (0)",
        },
    ]
}


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, transport: object, timeout: float) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        raise ConnectionError("permission denied")


class _GoodAsyncClient(_FakeAsyncClient):
    async def get(self, url: str) -> _FakeResponse:
        return _FakeResponse(_ROUTES[url])


def test_enabled_false_without_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "monitor_docker_socket", "")
    assert DockerService(socket_path="").enabled is False


def test_enabled_true_with_socket() -> None:
    assert DockerService(socket_path="/var/run/docker.sock").enabled is True


async def test_containers_returns_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.services.monitoring.docker_status as docker_pkg

    monkeypatch.setattr(docker_pkg, "AsyncClient", _GoodAsyncClient)
    service = DockerService(socket_path="/tmp/opencode/docker.sock")

    containers = await service.containers()
    assert len(containers) == 2
    assert containers[0]["Names"] == ["/news_engine_api"]
    assert containers[1]["State"] == "exited"


async def test_unhealthy_filters_stopped_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.services.monitoring.docker_status as docker_pkg

    monkeypatch.setattr(docker_pkg, "AsyncClient", _GoodAsyncClient)
    service = DockerService(socket_path="/tmp/docker.sock")

    bad = await service.unhealthy()
    assert len(bad) == 1
    assert bad[0]["Names"] == ["/news_engine_temporal"]


async def test_containers_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.services.monitoring.docker_status as docker_pkg

    monkeypatch.setattr(docker_pkg, "AsyncClient", _FakeAsyncClient)
    service = DockerService(socket_path="/tmp/docker.sock")

    with pytest.raises(DockerNotAvailableError):
        await service.containers()
