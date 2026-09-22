"""Unit tests for the system-monitor checks."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ConnectError

from src.api.models.monitoring import CheckResult
from src.api.services.monitoring import CheckContext
from src.api.services.monitoring.checks import (
    check_collections,
    check_digest_runs,
    check_disk_local,
    check_docker_containers,
    check_edge_tts,
    check_services,
    check_tts_server_disk,
    run_all_checks,
)
from src.api.services.monitoring.docker_status import DockerNotAvailableError
from src.core.config import settings

COLLECTIONS_TOML = """
[[collections]]
name = 'tech'
time = '08:00'
feed_urls = ['https://example.com/rss']

[[collections]]
name = 'health'
time = '09:30'
feed_urls = ['https://health.example.com/rss']
"""


def _write_collections(tmp_path: Path, content: str = COLLECTIONS_TOML) -> None:
    """Point settings.collections_file at a temp collections file."""
    path = tmp_path / "collections.toml"
    path.write_text(content, encoding="utf-8")
    settings.collections_file = str(path)


class _FakeResponse:
    """Minimal httpx response double."""

    def __init__(self, payload: object, content_type: str = "application/json") -> None:
        self._payload = payload
        self._content_type = content_type

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": self._content_type}


class _FakeAsyncClient:
    """httpx.AsyncClient double that resolves responses by URL."""

    def __init__(self, resolver: object, timeout: float | None = None) -> None:
        self._resolver = resolver

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        return self._resolver.resolve(url)


class _Resolver:
    """URL -> response mapping for the fake client."""

    def __init__(self) -> None:
        self.routes: dict[str, _FakeResponse] = {}
        self.fail: list[str] = []

    def resolve(self, url: str) -> _FakeResponse:
        if url in self.fail:
            raise ConnectError("connection refused")
        return self.routes.get(url, _FakeResponse({"status": "ok"}))


class _FakeCheckEnabled:
    """Serves the real enabled collection names for schedule reporting."""

    def __init__(self, names: list[str]) -> None:
        self.names = names

    async def list_schedule_ids(self) -> list[str]:
        return [f"daily-digest-{name}" for name in self.names]


async def test_check_docker_containers_disabled_socket_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty docker socket path yields a skipped check."""
    monkeypatch.setattr(settings, "monitor_docker_socket", "")
    ctx = CheckContext()
    result = await check_docker_containers(ctx)
    assert result.status == "skipped"


async def test_check_docker_containers_unavailable_socket_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable socket yields a skipped check, not an error."""
    monkeypatch.setattr(settings, "monitor_docker_socket", "/does/not/exist.sock")
    ctx = CheckContext(docker=AsyncMock())  # type: ignore[arg-type]
    ctx.docker.enabled = True
    ctx.docker.unhealthy = AsyncMock(side_effect=DockerNotAvailableError("no socket"))
    result = await check_docker_containers(ctx)
    assert result.status == "skipped"


async def test_check_docker_containers_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "monitor_docker_socket", "/var/run/docker.sock")
    docker = AsyncMock()
    docker.enabled = True
    docker.unhealthy = AsyncMock(return_value=[])
    ctx = CheckContext(docker=docker)  # type: ignore[arg-type]
    result = await check_docker_containers(ctx)
    assert result.status == "ok"


async def test_check_docker_containers_unhealthy_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "monitor_docker_socket", "/var/run/docker.sock")
    docker = AsyncMock()
    docker.enabled = True
    docker.unhealthy = AsyncMock(return_value=[{"names": ["news_engine_temporal"]}])
    ctx = CheckContext(docker=docker)  # type: ignore[arg-type]
    result = await check_docker_containers(ctx)
    assert result.status == "warning"
    assert "news_engine_temporal" in result.detail


async def test_check_edge_tts_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("edge_tts.list_voices", AsyncMock(return_value=[]))
    result = await check_edge_tts(CheckContext())
    assert result.status == "ok"


async def test_check_edge_tts_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> list:
        raise ConnectError("network down")

    monkeypatch.setattr("edge_tts.list_voices", boom)
    result = await check_edge_tts(CheckContext())
    assert result.status == "error"


async def test_check_tts_server_disk_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tts_api_base_url", "http://tts.example:8000/api/v1")
    monkeypatch.setattr(settings, "monitor_disk_threshold_percent", 80)
    resolver = _Resolver()
    resolver.routes["http://tts.example:8000/api/v1/disk"] = _FakeResponse(
        {"total": 1000, "free": 900, "used": 100}
    )

    monkeypatch.setattr(
        "src.api.services.monitoring.checks.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(resolver, kw.get("timeout", 10.0)),
    )
    result = await check_tts_server_disk(CheckContext())
    assert result.status == "ok"
    assert "90.0%" in result.detail


async def test_check_tts_server_disk_low_space_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tts_api_base_url", "http://tts.example:8000/api/v1")
    monkeypatch.setattr(settings, "monitor_disk_threshold_percent", 80)
    resolver = _Resolver()
    resolver.routes["http://tts.example:8000/api/v1/disk"] = _FakeResponse(
        {"total": 1000, "free": 100, "used": 900}
    )
    monkeypatch.setattr(
        "src.api.services.monitoring.checks.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(resolver, kw.get("timeout", 10.0)),
    )
    result = await check_tts_server_disk(CheckContext())
    assert result.status == "warning"
    assert "10.0% free" in result.detail


async def test_check_tts_server_disk_missing_fields_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tts_api_base_url", "http://tts.example:8000/api/v1")
    resolver = _Resolver()
    resolver.routes["http://tts.example:8000/api/v1/disk"] = _FakeResponse({"unexpected": 1})
    monkeypatch.setattr(
        "src.api.services.monitoring.checks.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(resolver, kw.get("timeout", 10.0)),
    )
    result = await check_tts_server_disk(CheckContext())
    assert result.status == "skipped"


async def test_check_tts_server_disk_skipped_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tts_api_base_url", "http://tts.example:8000/api/v1")
    resolver = _Resolver()
    resolver.fail.append("http://tts.example:8000/api/v1/disk")
    monkeypatch.setattr(
        "src.api.services.monitoring.checks.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(resolver, kw.get("timeout", 10.0)),
    )
    result = await check_tts_server_disk(CheckContext())
    assert result.status == "skipped"


async def test_check_services_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _Resolver()
    resolver.routes["http://api:8000/health"] = _FakeResponse({"status": "ok"})
    resolver.routes["http://temporal:7233/health"] = _FakeResponse({"status": "ok"})
    monkeypatch.setattr(
        "src.api.services.monitoring.checks.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(resolver, kw.get("timeout", 5.0)),
    )
    result = await check_services(CheckContext())
    assert result.status == "ok"


async def test_check_services_warns_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _Resolver()
    resolver.fail.append("http://api:8000/health")
    monkeypatch.setattr(
        "src.api.services.monitoring.checks.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(resolver, kw.get("timeout", 5.0)),
    )
    result = await check_services(CheckContext())
    assert result.status == "warning"
    assert "api" in result.detail


async def test_probe_sends_monitor_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared probe sends a descriptive UA so UA-hostile feeds pass (400 otherwise)."""
    captured: dict = {}

    class _CapturingClient(_FakeAsyncClient):
        def __init__(
            self,
            resolver: object | None = None,
            timeout: float | None = None,
            headers: dict | None = None,
            follow_redirects: bool = True,
        ) -> None:
            super().__init__(resolver or _Resolver(), timeout)
            captured["headers"] = headers

    monkeypatch.setattr("src.api.services.monitoring.checks.httpx.AsyncClient", _CapturingClient)
    from src.api.services.monitoring.checks import _probe

    result = await _probe("https://feeds.example.com/rss", timeout=10.0)
    assert result is None
    assert captured["headers"] == {"User-Agent": "news-engine-monitor/1.0"}


async def test_check_collections_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_collections(tmp_path)
    monkeypatch.setattr("src.api.services.monitoring.checks._probe", AsyncMock(return_value=None))
    ctx = CheckContext(temporal_admin=_FakeCheckEnabled(["tech", "health"]))  # type: ignore[arg-type]
    result = await check_collections(ctx)
    assert result.status == "ok"


async def test_check_collections_missing_schedule_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_collections(tmp_path)
    monkeypatch.setattr("src.api.services.monitoring.checks._probe", AsyncMock(return_value=None))
    ctx = CheckContext(temporal_admin=_FakeCheckEnabled(["tech"]))  # type: ignore[arg-type]
    result = await check_collections(ctx)
    assert result.status == "warning"
    assert "daily-digest-health" in result.detail


async def test_check_collections_parse_error(tmp_path: Path) -> None:
    _write_collections(tmp_path, "not = [valid toml")
    ctx = CheckContext(temporal_admin=_FakeCheckEnabled([]))  # type: ignore[arg-type]
    result = await check_collections(ctx)
    assert result.status == "error"


class _FakeRuns:
    def __init__(self, runs: list[dict]) -> None:
        self.runs = runs

    async def list_runs(self, collection: str = "", limit: int = 1) -> list[dict]:
        return [r for r in self.runs if not collection or r.get("collection") == collection][:limit]


async def test_check_digest_runs_ok(tmp_path: Path) -> None:
    _write_collections(tmp_path)
    runs = _FakeRuns(
        [
            {
                "collection": "tech",
                "status": "ok",
                "article_count": 5,
                "audio_path": "x.mp3",
                "completed_at": "2026-09-21T09:00:00+00:00",
            },
            {
                "collection": "health",
                "status": "no_new_articles",
                "article_count": 0,
                "audio_path": None,
                "completed_at": "2026-09-21T09:30:00+00:00",
            },
        ]
    )
    ctx = CheckContext(runs_repo=runs)  # type: ignore[arg-type]
    result = await check_digest_runs(ctx)
    assert result.status == "ok"


async def test_check_digest_runs_failed_warns(tmp_path: Path) -> None:
    _write_collections(tmp_path)
    runs = _FakeRuns(
        [
            {
                "collection": "tech",
                "status": "failed",
                "article_count": 0,
                "audio_path": None,
                "completed_at": "2026-09-21T09:00:00+00:00",
                "error": "llm timeout",
            }
        ]
    )
    ctx = CheckContext(runs_repo=runs)  # type: ignore[arg-type]
    result = await check_digest_runs(ctx)
    assert result.status == "warning"
    assert "tech: failed" in result.detail
    assert "health: never run" in result.detail


async def test_check_digest_runs_never_run_warns(tmp_path: Path) -> None:
    _write_collections(tmp_path)
    ctx = CheckContext(runs_repo=_FakeRuns([]))  # type: ignore[arg-type]
    result = await check_digest_runs(ctx)
    assert result.status == "warning"
    assert "never run" in result.detail


async def test_check_disk_local_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "monitor_disk_threshold_percent", 0)
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "news.db"))
    monkeypatch.setattr(settings, "tts_cache_dir", str(tmp_path / "cache"))
    result = await check_disk_local(CheckContext())
    assert result.status == "ok"


async def test_check_disk_local_warns_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "monitor_disk_threshold_percent", 99)
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "news.db"))
    monkeypatch.setattr(settings, "tts_cache_dir", str(tmp_path / "cache"))
    result = await check_disk_local(CheckContext())
    assert result.status == "warning"
    assert "below 99%" in result.detail


async def test_check_disk_local_missing_path_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "database_path", str(tmp_path / "does" / "not" / "exist" / "news.db")
    )
    result = await check_disk_local(CheckContext())
    assert result.status == "error"


async def test_run_all_checks_returns_registered_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_all_checks returns one CheckResult per registered check in order."""

    async def fake_a(ctx: CheckContext) -> CheckResult:
        return CheckResult(check="a", status="ok")

    async def fake_b(ctx: CheckContext) -> CheckResult:
        return CheckResult(check="b", status="ok")

    import src.api.services.monitoring.checks as checks_pkg

    checks_pkg.MONITOR_CHECKS[:] = [("a", fake_a), ("b", fake_b)]  # type: ignore[assignment]
    results = await run_all_checks()
    assert [r.check for r in results] == ["a", "b"]


async def test_run_all_checks_wraps_crashes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(ctx: CheckContext) -> CheckResult:
        raise RuntimeError("kaboom")

    import src.api.services.monitoring.checks as checks_pkg

    checks_pkg.MONITOR_CHECKS[:] = [("a", boom)]  # type: ignore[assignment]
    results = await run_all_checks()
    assert results[0].status == "error"
    assert "kaboom" in results[0].detail
