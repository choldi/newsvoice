"""Tests unitarios para middleware (RequestID y API Key)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.testclient import TestClient

from tts_api.middleware import APIKeyMiddleware, RequestIDMiddleware

# --- Helpers ---


async def dummy_handler(request: Request) -> Response:
    """Endpoint dummy para tests."""
    return JSONResponse({"ok": True, "request_id": getattr(request.state, "request_id", None)})


def _build_app(api_key: str = "") -> Starlette:
    """Construye una app Starlette de prueba con los middlewares."""
    app = Starlette()
    app.add_route("/test", dummy_handler)
    app.add_route("/api/v1/health", dummy_handler)
    app.add_route("/api/v1/disk", dummy_handler)
    app.add_route("/docs", dummy_handler)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(APIKeyMiddleware, api_key=api_key)
    return app


# --- RequestIDMiddleware tests ---


class TestRequestIDMiddleware:
    """Tests para RequestIDMiddleware."""

    def test_adds_request_id_from_header(self) -> None:
        """Test de que usa el X-Request-ID del header si se proporciona."""
        client = TestClient(_build_app())
        response = client.get("/test", headers={"X-Request-ID": "my-custom-id"})
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "my-custom-id"
        assert response.json()["request_id"] == "my-custom-id"

    def test_generates_request_id_if_missing(self) -> None:
        """Test de que genera un request_id si no se proporciona."""
        client = TestClient(_build_app())
        response = client.get("/test")
        assert response.status_code == 200
        request_id = response.headers["X-Request-ID"]
        assert len(request_id) == 36  # UUID format
        assert response.json()["request_id"] == request_id

    def test_adds_response_time_header(self) -> None:
        """Test de que agrega el header X-Response-Time."""
        client = TestClient(_build_app())
        response = client.get("/test")
        assert "X-Response-Time" in response.headers
        assert response.headers["X-Response-Time"].endswith("ms")


# --- APIKeyMiddleware tests ---


class TestAPIKeyMiddleware:
    """Tests para APIKeyMiddleware."""

    def test_no_api_key_allows_all(self) -> None:
        """Test de que sin API key configurada, todas las peticiones pasan."""
        client = TestClient(_build_app(api_key=""))
        response = client.get("/test")
        assert response.status_code == 200

    def test_valid_api_key(self) -> None:
        """Test de que API key válida permite acceso."""
        client = TestClient(_build_app(api_key="secret123"))
        response = client.get("/test", headers={"X-API-Key": "secret123"})
        assert response.status_code == 200

    def test_invalid_api_key(self) -> None:
        """Test de que API key inválida retorna 401."""
        client = TestClient(_build_app(api_key="secret123"))
        response = client.get("/test", headers={"X-API-Key": "wrong"})
        assert response.status_code == 401
        assert response.json()["error_type"] == "unauthorized"

    def test_missing_api_key(self) -> None:
        """Test de que sin API key configurada retorna 401."""
        client = TestClient(_build_app(api_key="secret123"))
        response = client.get("/test")
        assert response.status_code == 401

    def test_api_key_via_query_param(self) -> None:
        """Test de que API key funciona como query param."""
        client = TestClient(_build_app(api_key="secret123"))
        response = client.get("/test?api_key=secret123")
        assert response.status_code == 200

    def test_health_endpoint_bypasses_auth(self) -> None:
        """Test de que /health no requiere API key."""
        client = TestClient(_build_app(api_key="secret123"))
        response = client.get("/api/v1/health")
        assert response.status_code == 200

    def test_disk_endpoint_bypasses_auth(self) -> None:
        """Test de que /disk no requiere API key."""
        client = TestClient(_build_app(api_key="secret123"))
        response = client.get("/api/v1/disk")
        assert response.status_code == 200

    def test_docs_endpoint_bypasses_auth(self) -> None:
        """Test de que /docs no requiere API key."""
        client = TestClient(_build_app(api_key="secret123"))
        response = client.get("/docs")
        assert response.status_code == 200
