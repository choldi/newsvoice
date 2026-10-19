FROM python:3.12-slim AS base

WORKDIR /app

# Instalar dependencias del sistema para soundfile, pydub
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependencias primero (mejor cache de Docker)
# The three runtime engines are baked in so the image can switch between them by
# config alone (TTS_ENGINE / TTS_ENGINES): pocket_tts and edgetts cover the
# default routing, omnivoice is installed but stays unloaded unless a job pins
# it (and the host has a GPU; otherwise it stays disabled and only Modal is
# used for those backends).
COPY pyproject.toml uv.lock ./
ARG UV_EXTRAS="--extra pocket_tts --extra edgetts --extra omnivoice"
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev --no-install-project ${UV_EXTRAS} && \
    uv cache clean

# Copiar código fuente y el catálogo de voces versionado
COPY tts_api/ tts_api/
COPY voice_profiles.json ./
RUN uv sync --frozen --no-dev ${UV_EXTRAS}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/api/v1/health/live'); exit(0 if r.status_code==200 else 1)"

CMD ["uvicorn", "tts_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
