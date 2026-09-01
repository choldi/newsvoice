# Deployment Guide — AI News Digest Engine

This guide details instructions for deploying and running **news-engine**.

---

## 1. Prerequisites

- Server with Linux OS (Ubuntu 22.04 LTS or similar recommended)
- Docker & Docker Compose installed
- Local LLM inference server (e.g. `llama.cpp` server) accessible via HTTP API (`/v1/chat/completions`)

---

## 2. Configuration Setup

1. Clone the repository to the production server.
2. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
3. Update `.env` with production values:
   - `LLM_API_BASE`: URL of your llama.cpp instance
   - `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`

---

## 3. Starting Services

Using Docker Compose:
```bash
docker compose up -d --build
```

Verify service status:
```bash
docker compose ps
```

---

## 4. Health Checks

Check API health status:
```bash
curl http://localhost:8000/health
```
Expected response:
```json
{"status": "ok", "service": "news-engine-api"}
```

Check n8n health status:
```bash
curl http://localhost:5678/healthz
```

---

## 5. Rollback & Maintenance

To view API logs:
```bash
docker compose logs -f api
```

To stop services:
```bash
docker compose down
```

To restart API only:
```bash
docker compose restart api
```
