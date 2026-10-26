FROM python:3.12-slim AS builder

WORKDIR /app

# Instalar dependencias del sistema para soundfile, pydub y espeak-ng (Kokoro
# usa espeak-ng via misaki/espeakng-loader para el frontend G2P multiidioma).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Workspace metadata + the shared tts_contract lib, so `uv sync --package
# tts-api` resolves `tts-contract` from the monorepo root lock. The three
# runtime engines are baked in so the image can switch between them by config
# alone (TTS_ENGINE / TTS_ENGINES): pocket_tts and edgetts cover the default
# routing, omnivoice is installed but stays unloaded unless a job pins it (and
# the host has a GPU; otherwise it stays disabled and only Modal is used for
# those backends).
COPY pyproject.toml uv.lock ./
COPY libs/tts_contract ./libs/tts_contract
COPY apps/gateway/pyproject.toml apps/gateway/README.md ./apps/gateway/
ARG UV_EXTRAS="--extra pocket_tts --extra edgetts --extra omnivoice --extra kokoro"
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev --no-install-project --package tts-api ${UV_EXTRAS} && \
    uv cache clean

# Copiar código fuente y el catálogo de voces versionado. voice_profiles.json
# y storage/ se resuelven relativas al CWD (/app) en runtime.
COPY apps/gateway/tts_api ./apps/gateway/tts_api
COPY apps/gateway/voice_profiles.json ./voice_profiles.json
RUN uv sync --frozen --no-dev --package tts-api ${UV_EXTRAS}

FROM python:3.12-slim

# gcc/g++ are needed at runtime by the qwen3_tts worker: torch Inductor/
# Triton compiles CUDA kernels on first use and aborts with "Failed to find C
# compiler" without a toolchain. They are also harmless for the other engines.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    espeak-ng \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The workspace venv carries every dependency (incl. tts_contract) and the
# editable installs point at /app/..., so the whole /app tree must be carried
# over for the absolute .pth paths to resolve.
COPY --from=builder /app /app

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/api/v1/health/live'); exit(0 if r.status_code==200 else 1)"

CMD ["uvicorn", "tts_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
