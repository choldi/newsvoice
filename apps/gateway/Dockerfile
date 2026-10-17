FROM python:3.12-slim AS base

WORKDIR /app

# Instalar dependencias del sistema para soundfile, pydub
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependencias primero (mejor cache de Docker)
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev --no-install-project && \
    uv cache clean

# Copiar código fuente
COPY tts_api/ tts_api/
RUN uv sync --frozen --no-dev

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/api/v1/health/live'); exit(0 if r.status_code==200 else 1)"

CMD ["uvicorn", "tts_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
