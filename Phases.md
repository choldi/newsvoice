# PHASES.MD - Incremental Delivery Plan

Each phase is **independently shippable**. Do NOT start phase N+1 until N is running reliably for **3+ days**.

---

## PHASE 1 - MVP (Single Endpoint)

**Goal:** End-to-end flow working in 1 weekend.

**Scope:**
- [x] FastAPI app with ONE endpoint: `POST /mvp_digest`
- [x] Receives list of articles from n8n
- [x] Calls llama.cpp via LiteLLM
- [x] Returns 5+5 markdown
- [x] n8n workflow: `Schedule -> RSS -> /mvp_digest -> Telegram`
- [x] `docker-compose.yml` with n8n + api
- [x] `.env.example` documented

**Success criteria:** Receive a Telegram message every morning at **08:00** with 5 summaries + 5 headlines.

**Effort:** 4-6 hours.

**Skip:** DB, dedup, clustering, TTS.

---

## PHASE 2 - State and Deduplication

**Goal:** Never send the same article twice.

**Scope:**
- [x] SQLite schema: `articles`, `deliveries`
- [x] New endpoint: `POST /filter_new`
- [x] Replace `/mvp_digest` with `/summarize` (no RSS logic inside)
- [x] n8n workflow updated: `RSS -> /filter_new -> /summarize -> Telegram`
- [x] Unit tests for dedup logic (URL normalization, 7-day window)

**Success criteria:** Re-running the flow on the same RSS feed within 24h produces **zero duplicate deliveries**.

**Effort:** 6-8 hours.

---

## PHASE 3 - Event Clustering

**Goal:** Multiple articles about the same event become one story.

**Scope:**
- [ ] New endpoint: `POST /cluster`
- [ ] Fingerprint: `{entity, action, normalized_title_hash}`
- [ ] SQLite table: `clusters`
- [ ] Material-update detection (same cluster + new facts -> re-deliver)
- [ ] n8n workflow: `RSS -> /filter_new -> /cluster -> /summarize -> Telegram`
- [ ] Integration tests with fixture of 10 related + 5 unrelated articles

**Success criteria:** 5 articles about "OpenAI releases GPT-5" collapse into **1 digest entry**.

**Effort:** 10-14 hours.

---

## PHASE 4 - Multimodal Output (TTS)

**Goal:** Audio version of the digest for commute listening.

**Scope:**
- [ ] New endpoint: `POST /generate_tts`
- [ ] Use `edge-tts` (free, high quality) or local piper
- [ ] Cache audio by content hash in `data/tts_cache/`
- [ ] n8n workflow: parallel branch for text + audio
- [ ] Telegram node sends both text message and audio file

**Success criteria:** Receive both a text message and a 2-3 min **MP3** in Telegram.

**Effort:** 4-6 hours.

---

## PHASE 5 - Multiple Collections and Schedules

**Goal:** Separate digests for "Tech", "Health", "Finance".

**Scope:**
- [ ] `collections.toml` (inspired by Better Morning)
- [ ] Each collection = independent n8n workflow + independent schedule
- [ ] Shared DB, collection-scoped dedup
- [ ] Per-collection prompt templates

**Success criteria:** 3 independent Telegram digests at different times.

**Effort:** 6-8 hours.

---

## PHASE 6 (Optional) - Web UI and Multi-user

**Goal:** Manage sources and view history from browser.

**Scope:**
- [ ] FastAPI + HTMX or simple React frontend
- [ ] Auth (basic token)
- [ ] CRUD for collections and feeds
- [ ] History viewer with re-deliver button

**Success criteria:** Non-technical user can add an RSS feed without touching code.

**Effort:** 20-30 hours.

> **Only if Phase 1-5 are stable.**

---

## ANTI-PATTERNS TO AVOID

- Do NOT add clustering in Phase 1
- Do NOT use Postgres in Phase 1-3
- Do NOT build a web UI before Phase 5
- Do NOT replace n8n with Python schedulers (APScheduler/cron)
- Do NOT call LLM directly from n8n (always via Python endpoint)