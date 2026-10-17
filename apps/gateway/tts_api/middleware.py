"""Middleware para request_id, API key y rate limiting."""

from __future__ import annotations

import time
import uuid
from typing import ClassVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Agrega un request_id único a cada petición."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{elapsed_ms:.1f}ms"

        return response


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Valida API key opcional.

    Si settings.API_KEY está vacío, todas las peticiones pasan sin autenticación.
    Si tiene un valor, se requiere el header `X-API-Key` o query param `api_key`.
    Los endpoints públicos (sin auth): /docs, /redoc, /openapi.json, /metrics,
    /api/v1/health* y /api/v1/disk.
    """

    PUBLIC_PATHS: ClassVar[set[str]] = {
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/metrics",
        "/api/v1/disk",
    }

    def __init__(self, app, api_key: str = "") -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self._api_key:
            return await call_next(request)

        path = request.url.path

        # Endpoints públicos
        if path in self.PUBLIC_PATHS:
            return await call_next(request)
        if path.startswith("/api/v1/health"):
            return await call_next(request)

        # Verificar header o query param
        provided_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if provided_key == self._api_key:
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={
                "detail": "API key inválida o no proporcionada",
                "error_type": "unauthorized",
            },
        )
