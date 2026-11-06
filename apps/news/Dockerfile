# Stage 1: install the dependency set only
FROM python:3.11-slim AS builder

WORKDIR /app

# Workspace metadata + the shared tts_contract lib, so `uv sync --package
# news-engine` resolves `tts-contract` from the monorepo root lock (it is not
# published to PyPI).
COPY pyproject.toml uv.lock ./
COPY libs/tts_contract ./libs/tts_contract
COPY apps/news/pyproject.toml apps/news/README.md ./apps/news/
# --no-install-project: the app is imported as `src.*` from the CWD (/app) at
# runtime, so only the dependency set is needed in the venv.
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev --no-install-project --package news-engine && \
    uv cache clean

# Stage 2: runtime
FROM python:3.11-slim

# ffmpeg transcodes the synthesized WAV to M4A (AAC) before delivery so audio
# stays under Telegram's 50 MB limit and plays on Telegram/WhatsApp/Android/iOS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Setuptools <84 vendors wheel 0.45.1 and jaraco.context 5.3.0 in
# setuptools/_vendor (CVE-2026-24049, CVE-2026-23949). Bumping setuptools
# vendors the patched copies (wheel 0.46.3 / jaraco.context 6.1.0).
RUN pip install --no-cache-dir --upgrade "setuptools>=84.2.1"

WORKDIR /app

# The workspace venv carries every dependency (incl. tts_contract) and its
# editable .pth points at /app/libs/tts_contract, so the whole /app tree must
# be carried over for the absolute path to resolve. src/ is imported from the
# CWD; data/ and collections.toml are bind-mounted by compose.
COPY --from=builder /app /app
COPY apps/news/src ./src

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
