# Stage 1: install dependencies
FROM python:3.11-slim AS builder

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

# Stage 2: runtime
FROM python:3.11-slim

# ffmpeg transcodes the synthesized WAV to M4A (AAC) before delivery so audio
# stays under Telegram's 50 MB limit and plays on Telegram/WhatsApp/Android/iOS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ src/
COPY data/ data/
# The voice catalog is read from the working directory by the admin routes and
# by design_voice / run_voice_worker --role catalog.
COPY voice_profiles.json ./

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
