# Observability & Tracing Implementation Plan

This plan adds end-to-end tracing and logging across all components (API, services, workers/Temporal, LLM, TTS engine, Modal.com) with a UI for monitoring, filtering, level control, and export. Local-only (no OTLP). Cross-process visibility via file+tail with options documented.

## Phase 1: Core Infrastructure (Foundation)
Goal: Create context, logging extensions, tracing core, and config. No behavior changes to existing code paths.

### 1.1 New file: `src/core/context.py`
- **Purpose**: Contextvars for correlation/trace IDs across call chains (HTTP → services → Temporal → Modal → LLM/TTS).
- **Exports**:
  - Contextvars: `correlation_id_var`, `trace_id_var`, `span_id_var`, `component_var`, `process_var`, `execution_id_var`, `span_name_var`
  - Functions: `get_correlation_id()`, `set_correlation_id(id)`, `clear_correlation_id()`, `get_trace_id()`, `set_trace_context(trace_id, span_id)`, `clear_trace_context()`, `set_component(name)`, `get_component()`, `bind_context(**kw)`, `clear_context()`, `generate_correlation_id()`, `generate_trace_id()`
- **Notes**: Deterministic-safe (just contextvars). Include `process` (api/worker) default.

### 1.2 Extend: `src/core/config.py`
Add observability settings (pydantic Fields). New fields:
- `log_level`: str (exists) — keep. Add:
- `log_file_dir`: str = "data/logs"
- `log_file_name`: str = "news_engine.jsonl"
- `log_file_max_bytes`: int = Field(default=5_000_000, ge=100000)
- `log_file_backup_count`: int = Field(default=5, ge=1)
- `log_ring_size`: int = Field(default=10000, ge=1000)
- `log_include_caller`: bool = True
- `trace_enabled`: bool = True (local-only)
- `trace_sampling_ratio`: float = Field(default=0.1, ge=0.0, le=1.0)
- `trace_instrument_all`: bool = False (deep function tracing opt-in)
- `trace_capture_args`: bool = False
- `trace_capture_result`: bool = False
- `trace_max_arg_len`: int = Field(default=200, ge=0)
- `obs_export_dir`: str = "data/logs/exports"
- `obs_live_push_enabled`: bool = False (HTTP push from worker→API, optional)
- `obs_live_push_endpoint`: str = "http://api:8000/internal/logs"
- `obs_live_push_batch_size`: int = Field(default=50, ge=1)
- `obs_live_push_interval_ms`: int = Field(default=500, ge=10)
- `obs_live_push_secret`: str = "" (HMAC/internal)
- `obs_process_name`: str = "" (auto-set per process)

### 1.3 Extend: `src/core/logging.py` (create if missing pattern? doesn't exist; add new module)
Create `src/core/logging.py` (or extend existing via config; main.py configures directly). Better create `src/core/logging_setup.py` or `logging_ext.py`? Or add `src/core/logging.py` as new with full config. Since main.py does inline structlog.configure, we’ll centralize.
- **New file**: `src/core/logging.py`
- Features: structlog processors (add_log_level, TimeStamper iso, add contextvars: correlation_id/trace_id/span_id/component/process/span_name), caller info (module/function/lineno) via CallsiteParameterAdder or custom, JSONRenderer. 
- In-memory ring buffer: custom `structlog` processor/handler that appends to circular deque of size `log_ring_size` (stores dicts). Thread-safe (asyncio lock not needed for sync writes; ring buffer accessed read-only by async queries).
- Rotating file handler: writes JSONL to `log_file_dir/log_file_name` via `logging.handlers.RotatingFileHandler` (or direct). Ensure dir exists.
- Component registry: class/func to register components and get/set per-component levels (map component→level). Integrates with stdlib loggers.
- Process tagging: include `process` from context or env `obs_process_name`.
- Functions: `configure_logging(process_name="api")`, `get_ring_buffer()`, `clear_ring_buffer()`, `get_component_levels()`, `set_component_level(component, level)`, `register_component(name)`, `query_logs(**filters)` (sync scan over ring buffer + optional file tail read), `append_export()`.
- Note: keep deterministic/low overhead.

### 1.4 New file: `src/core/tracing.py`
- OTel local-only setup. Providers: `TracerProvider` with resource (service.name=news-engine, process). 
- Exporters: `ConsoleSpanExporter` (optional, off by default) + in-memory span store (custom `SpanProcessor` or store finished spans in deque by trace_id/correlation_id). No OTLP.
- Sampling: `TraceIdRatioBased(trace_sampling_ratio)` or parentbased.
- Helpers: `get_tracer(component)`, `trace_function(component=None, capture_args=False, capture_result=False, max_arg_len=200)` decorator (sync/async), `start_span(name, **attrs)`, `inject_trace_context()` returns traceparent, `extract_trace_context(headers)` (for future), `get_inmem_spans(trace_id=None, correlation_id=None, limit=200)`, `clear_spans()`.
- Link to contextvars: on span start/end set trace_id/span_id in context.
- Temporal note: don’t call from workflows; interceptors handle workflows.

### 1.5 Update: `src/api/main.py`
- Import and call centralized logging: `from src.core.logging import configure_logging; configure_logging(process_name="api")` (replace inline structlog.configure). 
- Add correlation ID middleware: create middleware that generates `correlation_id` if missing (header `X-Correlation-ID` or uuid), binds to contextvars, sets response header `X-Correlation-ID`. Also extracts traceparent if present (`traceparent` header).
- Add request logging middleware: log request start/end with method, path, status_code, duration_ms, correlation_id, component="api.middleware".
- Keep existing CORS/admin middleware order sensible.

### 1.6 Update: `src/workers/client.py` / `src/workers/run_worker.py`
- In `configure_logging()` also call centralized `configure_logging(process_name="worker")` (or extend). Keep Temporal SDK routing. Ensure structlog uses same processors + ring buffer if shared process (worker separate). 
- Tag process="worker".

### 1.7 New file: `src/api/routes/logs_api.py` (internal/logs push + maybe internal)
- For cross-process live push (option C): `POST /internal/logs` accepts batch of log records (JSON). Requires `obs_live_push_secret` header/HMAC. Stores into API ring buffer. Mark as internal (not under /api public except auth not needed if secret; but behind network/internal). Also supports stateless? 
But better put under internal prefix not exposed publicly; add to main.py if enabled.

### 1.8 New models: `src/api/models/logs.py` (Pydantic)
- `LogLevel(str, Enum)`: TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL
- `LogRecordIn/Out`, `LogQuery`, `LogLevelUpdate`, `LogLevelItem`, `TraceSpanOut`, `TraceTimelineItem`.

Phase 1 deliverables: core modules compile, existing tests still pass, no functional UI yet.
## Phase 2: Instrument API, Services, LLM, TTS (caller-side)
Goal: Propagate correlation IDs and add spans/logs across routes/services/core.

### 2.1 Instrument `src/core/llm.py`
- Wrap LiteLLM calls with `@trace_function(component="core.llm")` or manual spans: attributes `llm.model`, `llm.temperature`, `llm.max_tokens`, `llm.duration_ms`, `llm.tokens` if available (avoid logging full prompts/responses; log lengths). 
- Inject correlation_id into logs. 
- Do not change external API.

### 2.2 Instrument `src/api/services/tts.py`
- Add spans around `synthesize_mp3`, cache lookup/write, backend dispatch. Component `tts.engine` (local edge path). Log timings, voice, language, backend, char_count, cached (bool), audio_path, duration_estimate.
- Include correlation_id.

### 2.3 Instrument `src/api/services/tts_backends.py`
- Span around gateway/Modal calls (caller-side). Component `tts.backends`. Attributes: backend type, voice, chars, timeout, success.
- For Modal paths: component tags include `modal.tts`/`modal.client` as appropriate.

### 2.4 Instrument Modal client: `src/core/modal_client.py`
- Wrap each function (`modal_tts`, `modal_zonos2_tts`, `upload_voice`, `modal_judge`) with span + structured log. Component `modal.client`. Attributes: `modal.function`, `modal.app`, non-secret metadata. Propagate correlation_id (pass as arg if Modal receives? Modal functions in this repo take specific args; we can add optional correlation_id/traceparent only if we also update modal_app.py signatures? Or include in logs only; for end-to-end caller-side span is enough). Keep existing signatures unchanged unless Modal side updated. Caller-side spans sufficient for local view.

### 2.5 Instrument services
Key services: `rss.py`, `dedup.py`, `cluster.py`, `summarize.py`, `deliver.py`, `logic.py`, `narration.py` (relevant parts), `doc_tts.py`, `lesson.py`, `tts_lease.py`. Add `@trace_function` on public methods. Component names: `api.services.<name>`. Log timings, input sizes, outcomes.

### 2.6 Instrument routes
In route handlers, ensure correlation_id from middleware is used. Add spans per handler? Or rely on FastAPI instrumentation. Can add `@trace_function(component=f"api.routes.{name}")` for complex handlers. Keep stateless.

### 2.7 FastAPI auto-instrumentation (optional)
Add `opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app(app)` if OTel available; but we control spans. Can do manually first to match component taxonomy.

Phase 2: spans cover API→services→LLM/TTS/Modal caller paths. All logs include correlation_id/component/process.
## Phase 3: Temporal Integration (determinism preserved)
Goal: Propagate correlation_id/trace context across workflow→activities. Workflows stay deterministic.

### 3.1 Update activities to accept/propagate context
- Modify activity signatures that form pipeline boundaries to accept optional `correlation_id: str | None = None` and optional `traceparent: str | None = None` (or dict). Add as trailing optional params to avoid breaking existing calls? Or pass via `args` with new fields. But we must update calls.
- At start of each activity: set contextvars (`correlation_id`, trace context if traceparent) before calling services. Clear after.
- Activities already call services directly – services now see context.

### 3.2 Update workflows to pass context
- In `DigestPipelineWorkflow.run`, `UrlSummarizeWorkflow`, others: get `correlation_id` from trigger? Or generate at workflow start if missing and pass down. Also pass `traceparent` if available. But workflows cannot read headers; pass as input args. 
- For scheduled/manual triggers: generate correlation_id at trigger boundary (`src/workers/trigger.py`, `schedule.py` entry or when starting workflow via client) and pass as first-class arg or in config? Add optional `correlation_id` param to workflow `run()` methods? Keep backward compatible (default None, generate if missing? not deterministic to generate inside workflow except use workflow.uuid-like; better pass from client).
- Update `workflow.execute_activity` calls to include `correlation_id`/`traceparent` in args list (multi-arg via args=[...]). Respect existing order.

### 3.3 Temporal client/worker interceptors (OTel)
- In `src/workers/client.py` build_client: add OTel interceptors if `trace_enabled`. In Worker creation (`run_worker`), add tracing interceptor. Use `temporalio.contrib.opentelemetry.TracingInterceptor` if available in SDK; otherwise skip (local-only view still via logs). SDK is temporalio 1.32.0 – check contrib availability.
- Ensure no non-deterministic OTel calls inside workflows.

### 3.4 Trigger/schedule boundaries
- `src/workers/trigger.py`, `src/workers/schedule.py` (and any code starting workflows): generate correlation_id, inject into workflow input, set in logs.

Phase 3: end-to-end correlation across Temporal preserved; deterministic.

## Phase 4: UI APIs for Logs/Traces (REST + SSE)
Goal: Add logs/traces endpoints under API. Integrate with existing UI routes style.

### 4.1 New routes: `src/api/routes/logs.py`
Endpoints:
- `GET /api/logs/levels` → current per-component levels
- `POST /api/logs/levels` (form or JSON) → set `{component, level}` (admin-protected? use existing admin middleware? under /api but maybe require admin; or read-only by default; level changes affect runtime)
- `GET /api/logs/query` – query ring buffer + file tail. Params: limit, offset, since_id, since_ts, level[], component[], text, correlation_id, trace_id, module, func, format (json|jsonl), download (bool). Return paged results.
- `GET /api/logs/stream` – SSE. Params: filters same. Streams new records; supports Last-Event-ID/since_id.
- `POST /api/logs/file/redirect` – toggle file appender state (enable/disable) or just info. Returns {enabled, path}.
- `GET /api/logs/export` – export filtered view to server file in `obs_export_dir`, returns download URL/path/filename.
- `GET /api/traces` – recent traces (grouped by trace_id/correlation_id) from in-mem spans
- `GET /api/traces/{trace_id}` – timeline (spans + linked logs). Need to correlate by trace_id (logs include trace_id/span_id)

Also internal: `POST /internal/logs` (see 1.7) for cross-process push (HMAC secret). Mount conditionally.

### 4.2 Update `src/api/main.py`
- Include `logs_router` (from routes.logs). Public under /api; internal routes mounted separately or in same router with prefix.

### 4.3 Logging query layer
In `src/core/logging.py`, implement `query_logs()` reading from ring buffer (in-mem) and optionally tailing recent lines from log files (for cross-process history). Merge by timestamp+log_id+process. Support since_id/ts.

Phase 4: APIs ready. No UI templates yet.
## Phase 5: UI Integration (monitor/logs view)
Goal: Add Logs/Monitor UI page using existing Jinja2 + HTMX style (matches routes_ui.py, base.html).

### 5.1 Add template: `src/api/templates/logs.html`
- Page with: level controls per component (dropdowns), filter by component (multi-select), level filter, text/correlation_id/trace_id search, refresh configurable (off/500/1000/2000/5000), tail/follow toggle, redirect-to-file toggle, export (JSONL/TXT), view toggle (Logs/Traces).
- Use HTMX for partial updates/SSE or periodic polling fallback. SSE endpoint /api/logs/stream consumed via EventSource.
- Table: ts, level, component, process, correlation_id, trace_id/span_id, module/func/line, message, duration_ms.

### 5.2 Update `src/api/routes_ui.py`
- Add route `GET /ui/logs` → render logs.html. Include in nav? Add nav link in base.html? Check base.html structure (has nav). Add “Logs/Monitor” link if appropriate (admin area).
- Pass component list from registry to template.

### 5.3 Update `src/api/templates/base.html` (nav)
- Add link to `/ui/logs` in navigation (consistent with existing style).

### 5.4 Static assets (optional)
- If needed, minimal JS for SSE/EventSource, filters. Can inline in template.

Phase 5: UI live monitor with filters, refresh, redirect, export. Cross-process via file+tail.

## Phase 6: Testing & Hardening
- Unit tests: context, logging (ring buffer, query), tracing decorator, levels.
- Integration: logs endpoints (query/stream/levels). Ensure SSE headers correct.
- Regression: all existing pytest still pass. No behavior change to pipeline.
- Ruff: format+check. Pytest -q.

## Phase 7: Documentation & Commit
- Update `CONVENTIONS.md` briefly (observability: correlation_id, structlog, local tracing). Don’t duplicate.
- Update `Phases.md` only if active phase matches? User says proceed with Phase 1; mark progress as we go.
- Commit per logical chunk (Phase 1, 2, 3...).

## Dependencies
Already have: `structlog`, `fastapi`, `temporalio`, `modal`. Need OTel deps? Check if present: `opentelemetry-api`, `sdk`, `instrumentation-fastapi`? Not in pyproject.toml. Add:
- `opentelemetry-api>=1.24.0`
- `opentelemetry-sdk>=1.24.0`
- `opentelemetry-instrumentation-fastapi>=0.45b0`
- `opentelemetry-propagator-b3>=1.24.0` (optional) — maybe not needed. Local-only only.

But follow rule: ask before adding deps? User says “produce file-by-file plan…” and proceed with Phase 1. Adding OTel deps is necessary for tracing. Propose minimal set. Add to pyproject.toml in Phase 1.1a? Or note. Include in plan.

### Add to pyproject.toml (dependencies)
Add:
- "opentelemetry-api>=1.27.0",
- "opentelemetry-sdk>=1.27.0",
- "opentelemetry-instrumentation-fastapi>=0.48b0",

(versions compatible with Python 3.11/FastAPI). No OTLP exporter.

## Phase 2: Instrument API, Services, LLM, TTS (caller-side)
File-by-file edits:
- `src/core/llm.py`: wrap LiteLLM calls with `@trace_function(component="core.llm")`. Log model/temp/max_tokens/duration_ms/tokens (non-sensitive). Keep existing API.
- `src/api/services/tts.py`: add spans around `synthesize_mp3`, cache lookup/write, backend dispatch. Component `tts.engine`. Attributes: backend, voice, language, char_count, cached, duration_estimate.
- `src/api/services/tts_backends.py`: spans for gateway/Modal dispatch. Component `tts.backends`/`modal.tts`.
- `src/core/modal_client.py`: spans around `modal_tts`, `modal_zonos2_tts`, `upload_voice`, `modal_judge`. Component `modal.client`. Propagate correlation_id only via logs/context (no signature change unless Modal updated).
- `src/api/services/rss.py`, `dedup.py`, `cluster.py`, `summarize.py`, `deliver.py`, `logic.py`, `narration.py`, `doc_tts.py`, `lesson.py`, `tts_lease.py`: add `@trace_function(component="api.services.<name>")` to public async/sync methods.
- Routes: add spans or rely on middleware; optional `@trace_function(component="api.routes.*")`.

## Phase 3: Temporal Integration (deterministic)
- Activities (`src/workers/activities.py`): accept optional `correlation_id` and `traceparent` in relevant activity signatures (append optional params). At start: set contextvars from input; clear at end. Component `workers.activities`.
- Workflows (`src/workers/workflows.py`): pass context through `execute_activity` args. Generate corr id at client boundary (trigger/schedule) not inside workflow. Deterministic.
- Client/Worker (`src/workers/client.py`, `src/workers/run_worker.py`): add OTel interceptors when `trace_enabled`. Propagate traceparent via activity inputs if available.
- Triggers: `src/workers/trigger.py`, `src/workers/schedule.py` (and any client code starting workflows): generate correlation_id, pass to workflow, log.

## Phase 4: UI APIs for Logs/Traces (extended)
- `src/api/routes/logs.py`: add SSE `GET /api/logs/stream` (filters), `POST /internal/logs` (HMAC push), `GET /api/traces`, `GET /api/traces/{trace_id}`, `GET /api/logs/export`. Implement cross-process merge (ring + file tail). Add component listing.

## Phase 5: UI Templates (logs view)
- `src/api/templates/logs.html`: full log viewer (filters, refresh, tail, export, trace view). HTMX/EventSource.
- `src/api/templates/base.html`: add nav link to `/ui/logs`.
- `src/api/routes_ui.py`: add `GET /ui/logs`.

## Phase 6–7: Testing/Docs as above
