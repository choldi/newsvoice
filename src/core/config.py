"""Configuration settings for news-engine using pydantic-settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    llm_api_base: str = "http://localhost:8080/v1"
    llm_model: str = "openai/local-model"
    # Credential for a hosted provider pointed at by llm_api_base. Empty (the
    # default) keeps local llama.cpp endpoints working without an Authorization
    # header; Phase 7.13 uses it for the 'api' verification backend.
    llm_api_key: str = ""
    llm_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    llm_timeout_seconds: int = Field(default=1800, ge=1)
    llm_max_tokens: int = Field(default=4096, gt=0)
    llm_disable_thinking: bool = True
    # --- Phase 7.13: LLM output verification -------------------------------
    # Global kill switch. The per-collection opt-in is `llm_verify` in
    # collections.toml; this flag can only turn every collection off.
    llm_verify_enabled: bool = True
    # Faithfulness score (supported claims / total claims) a digest must reach.
    # Verified against known-good digests before being tightened; see Deploy.md.
    llm_verify_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    # Share of numbers/dates/percentages that must exist verbatim in a source
    # article. This check needs no LLM, so it stays at 1.0: a fabricated figure
    # is the failure mode worth blocking on.
    llm_verify_groundedness_threshold: float = Field(default=1.0, ge=0.0, le=1.0)
    # Which LLM judges the claims: 'local' (llm_api_base), 'modal' (a Llama-3
    # class judge on Modal) or 'api' (a hosted OpenAI-compatible provider).
    llm_verify_backend: str = "local"
    # Judge model. Empty means "reuse llm_model" for the local backend,
    # `modal_judge_model` for the Modal backend and `llm_verify_api_base`'s own
    # default model for 'api'.
    llm_verify_model: str = ""
    # OpenAI-compatible endpoint for the 'api' backend. Empty reuses llm_api_base,
    # which is usually your local llama.cpp: set this to point the judge at a
    # hosted provider without moving the digest generation off the local model.
    llm_verify_api_base: str = ""
    # API key for the 'api' backend (Groq, OpenRouter, ...). Never logged.
    llm_verify_api_key: str = ""
    # A digest shorter than this many claims is judged on groundedness alone:
    # a score over one or two claims is noise.
    llm_verify_min_claims: int = Field(default=3, ge=0)
    llm_verify_max_claims: int = Field(default=40, ge=1)
    llm_verify_claims_per_call: int = Field(default=5, ge=1)
    # Sources sent to the judge. The judge prompt repeats them on every batch, so
    # they are capped; the free groundedness check always uses the full text.
    llm_verify_max_sources: int = Field(default=40, ge=1)
    llm_verify_source_chars: int = Field(default=1200, ge=100)
    # --- Phase 7.11: long-document TTS --------------------------------------
    # Kill switch for POST /api/admin/documents/tts.
    doc_tts_enabled: bool = True
    # Dedicated task queue: a 300-page render must not occupy the digest
    # worker's activity slots.
    doc_tts_task_queue: str = "news-engine-doc-tts-queue"
    # Upload cap in bytes; the body is rejected before extraction.
    doc_tts_max_file_bytes: int = Field(default=20_000_000, ge=1024)
    # Extracted-text cap in characters (~300 pages). Over it the job fails with
    # an explicit error instead of silently shipping a truncated book.
    doc_tts_max_source_chars: int = Field(default=600_000, ge=10_000)
    # Characters per edge-tts request; the chunks are joined into one MP3.
    doc_tts_chunk_chars: int = Field(default=4000, ge=500)
    # Chunks synthesized concurrently inside a single activity.
    doc_tts_concurrency: int = Field(default=4, ge=1, le=16)
    # Uploads, extracted text, chunk cache and finished audio (shared ./data
    # volume, so the API and the doc-tts worker see the same files).
    doc_tts_dir: str = "data/doc_tts"
    # HMAC key for the shareable audio links. Empty disables signed links and
    # the submit endpoint answers 503: a document audio without its URL is
    # useless, and an unguessable URL is the only auth a link has.
    doc_tts_link_secret: str = ""
    doc_tts_link_ttl_seconds: int = Field(default=604_800, ge=60)
    # --- Phase 7.12: language teacher ---------------------------------------
    # Kill switch for POST /api/admin/lessons.
    teacher_enabled: bool = True
    # Dedicated queue served by the teacher-worker compose service: an LLM call
    # plus a full narration render must not occupy the digest worker.
    teacher_task_queue: str = "news-engine-teacher-queue"
    # Notes upload cap (bytes) and extracted notes cap (characters).
    teacher_max_file_bytes: int = Field(default=5_000_000, ge=1024)
    teacher_max_notes_chars: int = Field(default=60_000, ge=1000)
    # Above this duration the audio is NOT attached: the lesson text carries a
    # signed URL instead (Phase 7.12 decision, configurable).
    teacher_max_audio_seconds: float = Field(default=300.0, ge=10.0)
    # Uploads, lesson text and job metadata (shared ./data volume).
    teacher_dir: str = "data/lessons"
    # Signed lesson-audio links share doc_tts_link_secret: one secret for every
    # shareable audio URL the project hands out.
    teacher_link_ttl_seconds: int = Field(default=604_800, ge=60)
    # --- Phase 7.10: single-URL summary -------------------------------------
    # Kill switch for POST /api/summarize/url (the endpoint stays behind the
    # admin auth either way; this closes it without a code change).
    url_summary_enabled: bool = True
    # Timeout for the HTTP GET of the article page.
    url_fetch_timeout: int = Field(default=30, ge=1)
    # Reject any response larger than this (the body is all we need).
    url_fetch_max_bytes: int = Field(default=5_000_000, ge=10_000)
    # Body characters handed to the prompt; longer articles are truncated.
    url_summary_max_chars: int = Field(default=12_000, ge=500)
    # Per-source reliability. A feed without an explicit value scores
    # `source_reliability_default`; below `min_source_reliability` the source is
    # dropped before clustering.
    source_reliability_default: float = Field(default=1.0, ge=0.0, le=1.0)
    min_source_reliability: float = Field(default=0.5, ge=0.0, le=1.0)
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, lt=65536)
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_from_name: str = "News Engine"
    # WhatsApp delivery via a self-hosted Baileys gateway (docker-compose
    # service named "there"; blakegt/whatsapp-web-api-rest listening on :8085).
    # Fail-closed: with no api_token configured the channel is disabled and a
    # digest never blocks on it; numbers must be in the allowlist (groups are
    # always allowed).
    whatsapp_api_url: str = "http://there:8085"
    whatsapp_api_token: str = ""
    whatsapp_allowlist: list[str] = Field(
        default_factory=list,
        description="E.164 numbers allowed to receive digests (groups exempt)",
    )
    # OpenClaw-style human typing delay between sends:
    # "off"=none, "natural"=800-2500ms, "custom"=[custom_min, custom_max].
    whatsapp_delay_mode: str = "natural"
    whatsapp_delay_custom_min_ms: int = Field(default=2000, ge=0)
    whatsapp_delay_custom_max_ms: int = Field(default=8000, ge=0)
    # Typing indicator while 'writing': "composing" or "none".
    whatsapp_typing_indicator: str = "composing"
    # Minimum cool-down per recipient between consecutive sends (milliseconds).
    whatsapp_recipient_cooldown_ms: int = Field(default=300, ge=0)
    api_port: int = Field(default=8000, ge=1, lt=65536)
    api_request_timeout: int = Field(default=120, ge=1)
    log_level: str = "INFO"
    database_path: str = "data/news.db"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "news-engine-task-queue"
    # The voice catalog lives in its own namespace so the remote GPU worker only
    # ever sees voice traffic. A Temporal activity always executes in the
    # namespace of the workflow that scheduled it, so the workflow, the
    # store/status activities and the design activity all live here
    # (see src/workers/run_voice_worker.py).
    temporal_voices_namespace: str = "news-engine-voices"
    # Phase 7.9b splits the podcast generation by backend, one namespace each:
    # the digest pipeline stays in ``temporal_namespace`` and no longer owns
    # the narration render, so local and Modal renders no longer share history
    # with it (a Temporal activity always executes in the namespace of the
    # workflow that scheduled it, so the render had to become its own
    # workflow started from a dispatch activity). See
    # src/workers/podcast_target.py.
    temporal_podcast_local_namespace: str = "news-engine-podcast-local"
    temporal_podcast_modal_namespace: str = "news-engine-podcast-modal"
    voice_design_task_queue: str = "news-engine-voice-design-queue"
    voice_catalog_task_queue: str = "news-engine-voice-catalog-queue"
    # The design worker runs on another box, so a reference reaches the server
    # over HTTP instead of through Temporal history: a 24-voice batch put ~20 MB
    # of gzipped WAV in one workflow and the server terminated it with
    # "Workflow history size exceeds limit". The activity POSTs the bytes to
    # this base URL; the workflow carries only the digest.
    voice_staging_url: str = "http://localhost:8000"
    # Shared key for POST /api/internal/voices/staged/{name}. Empty disables
    # the endpoint (503), which is the default: staging is opt-in.
    voice_staging_api_key: str = ""
    rss_feed_urls: list[str] = []
    collections_file: str = "collections.toml"
    # How often (minutes) the collections-config monitor workflow wakes to check
    # whether collections.toml changed and re-register the Temporal schedules.
    collections_monitor_interval_minutes: int = Field(default=5, ge=1)
    # How often (minutes) the system-monitor workflow wakes to run the checks.
    monitor_interval_minutes: int = Field(default=15, ge=1)
    # Retention window (days) for monitor events in SQLite and JSONL log files.
    monitor_retention_days: int = Field(default=3, ge=1)
    monitor_log_retention_days: int = Field(default=3, ge=1)
    # Directory where the daily-rotated JSONL monitor logs are written.
    monitor_log_dir: str = "data/logs"
    # Alert delivery for the system monitor. Defaults to Telegram using
    # monitor_telegram_chat_id (or the global telegram_chat_id). Supported
    # channels: telegram, email, whatsapp. Empty list disables alerts.
    monitor_notify_channels: list[str] = Field(default_factory=lambda: ["telegram"])
    monitor_telegram_chat_id: str = ""
    monitor_email_recipients: list[str] = Field(default_factory=list)
    monitor_whatsapp_recipients: list[str] = Field(default_factory=list)
    monitor_whatsapp_group_id: str | None = None
    # Emit an alert when free disk space drops below this percentage
    # (e.g. 20 = warn when the volume is over 80% full).
    monitor_disk_threshold_percent: int = Field(default=20, ge=1, le=99)
    # Send a daily system health summary (default) plus a per-run confirmation
    # when monitor_alert_on_run is enabled.
    monitor_daily_summary: bool = True
    monitor_alert_on_run: bool = False
    # TTS server disk probe: <tts_api_base_url>/<monitor_tts_disk_path> must
    # return {"total": int, "free": int, "used": int} in bytes.
    monitor_tts_disk_path: str = "disk"
    # Path to the Docker Engine UNIX socket used to probe container health.
    # Empty disables the docker_containers check.
    monitor_docker_socket: str = "/var/run/docker.sock"
    tts_cache_dir: str = "data/tts_cache"
    tts_api_base_url: str = "http://debwk.elver-exponential.ts.net:8000/api/v1"
    tts_api_key: str = ""
    # Kill switch for the admin TTS HTTP endpoint (POST /api/admin/tts). The
    # Temporal activities call the service directly, so setting this to false
    # only closes the HTTP surface. The former public POST /api/generate_tts
    # always answers 410 Gone.
    tts_api_enabled: bool = True
    tts_language: str = "es"
    tts_voice: str = "es-MX-JorgeNeural"
    tts_max_chars: int = Field(default=5000, gt=0)
    # Per-request timeout for a single Edge TTS synthesis call.
    tts_request_timeout: int = Field(default=600, ge=1)
    tts_podcast_behavior: str = ""
    tts_podcast_name: str = ""
    # Transcode the synthesized WAV to M4A (AAC) before delivery so the file is
    # small enough for Telegram's 50 MB limit and plays on Telegram, WhatsApp,
    # Android, and iPhone. ffmpeg is installed in the Docker image.
    ffmpeg_path: str = "ffmpeg"
    tts_audio_bitrate_kbps: int = Field(default=48, ge=16, le=320)
    admin_api_token: str = ""
    # --- Admin GUI authentication ---
    # When admin_username is set, the login form uses username/password instead
    # of the legacy ADMIN_API_TOKEN. Generate admin_password_hash with:
    # python -c "import bcrypt; print(bcrypt.hashpw(b'password', bcrypt.gensalt()).decode())"
    admin_username: str = ""
    admin_password_hash: str = ""

    # --- Narration (Phase 7) ------------------------------------------------
    # Master switch for the multi-speaker narration pipeline. Collections opt in
    # individually from Phase 7.3; this flag is the global kill switch.
    narration_enabled: bool = False
    # TTS gateway that serves the narration cast. Kept separate from
    # tts_api_base_url on purpose: the tts_* namespace talks to Edge directly
    # (except VoicesService) and repointing it would silently move the voices
    # GUI to another server. Empty means "reuse tts_api_base_url at read time".
    narration_api_base_url: str = ""
    narration_api_key: str = ""
    # Task queue served by the dedicated narration worker.
    narration_task_queue: str = "news-engine-narration-task-queue"
    # Phase 7.8 splits narration by backend: the local queue serves the
    # self-hosted gateway, the modal queue serves the GPU backends and holds
    # the Modal credentials, so the two scale and restart independently.
    # ``narration_task_queue`` is kept as an alias of the local queue: Phase
    # 7.9b gave the local side its own namespace and a dedicated worker, and
    # the two names used to point at different queues with only one worker.
    narration_local_task_queue: str = "news-engine-narration-task-queue"
    narration_modal_task_queue: str = "news-engine-narration-modal-task-queue"
    # Narration audio lives outside tts_cache so WAV/M4A files and their
    # .meta.json sidecars cannot collide with the digest TTS cache.
    narration_cache_dir: str = "data/narration"
    narration_music_dir: str = "data/narration/music"
    narration_language: str = "es"
    narration_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    narration_pause_ms: int = Field(default=400, ge=0, le=5000)
    # The gateway does not chunk long text and enforces a concurrency limit, so
    # requests are split client-side and the cap must be respected.
    narration_max_chars_per_request: int = Field(default=1500, ge=100, le=10_000)
    narration_max_concurrency: int = Field(default=2, ge=1, le=8)
    # The conversations strategy renders the whole dialogue in one gateway
    # request. Long requests risk timeouts and degrade on CPU, so the client
    # splits them into blocks of at most this many characters. Cuts fall
    # between turns, never inside a sentence.
    narration_conversation_max_chars_per_block: int = Field(default=4000, ge=500, le=20_000)
    # Hard cap on turns per block, so a block of many short turns stays small.
    narration_conversation_max_turns_per_block: int = Field(default=10, ge=2, le=60)
    # A trailing block with fewer turns than this is merged into the previous
    # one, so the render does not end with a tiny request.
    narration_conversation_min_block_turns: int = Field(default=2, ge=1, le=20)
    # Retries per block. The conversations strategy has no Edge fallback, so a
    # single failed turn would otherwise kill the whole render.
    narration_conversation_block_retries: int = Field(default=1, ge=0, le=5)
    # Generous timeout: the gateway has no server-side timeout, and a long
    # multi-turn conversation can take minutes on CPU.
    narration_request_timeout: int = Field(default=300, ge=1)
    # Fall back to a direct Edge TTS call when the gateway fails.
    narration_fallback_to_edge: bool = True
    # Fall back to Edge TTS for dialog/podcast renders (2+ voices or conversations).
    # When False, a dialog render will fail with an alert instead of silently
    # substituting both cloned voices with the same stock Edge voice.
    # Overrides narration_fallback_to_edge for dialog renders.
    narrations_dialog_fallback_to_edge: bool = False
    narration_audio_bitrate_kbps: int = Field(default=64, ge=16, le=320)

    # --- Modal (Phase 7.8) --------------------------------------------------
    # Kill switch for every Modal call. While False the Modal code path is
    # unreachable: no .spawn(), no spend, and narration stays on the local
    # queue. The hard budget caps land in a later commit; this flag is what
    # makes the dispatch safe to merge before they do.
    modal_enabled: bool = False
    # Gate for the ZONOS2 backend. ZONOS2 is implemented but inactive until
    # scripts/benchmark_zonos.py passes every criterion with a real voice.
    zonos2_enabled: bool = False
    modal_app_name: str = "news-engine"
    modal_token_id: str = ""
    modal_token_secret: str = ""
    modal_volume_name: str = "news-engine-voices"
    modal_gpu: str = "L4"
    # Model the Phase 7.13 verification judge runs on Modal. An ungated 8B by
    # default: Llama-3.3-70B is gated on HuggingFace and does not fit an L4
    # comfortably, so switching to it means accepting the license and quantizing.
    modal_judge_model: str = "Qwen/Qwen3-8B"
    # Qwen3-TTS runs in its own Modal image (Python 3.12, qwen-tts), so the
    # package never enters pyproject.toml and the local path needs no GPU.
    qwen_tts_base_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    qwen_tts_design_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    qwen_tts_max_new_tokens: int = Field(default=2048, ge=256, le=8192)
    qwen_tts_batch_size: int = Field(default=8, ge=1, le=32)
    # Voice design runs on the GPU box (Phase 7.9), not in the container. sdpa is
    # the default because designing a batch of short clips does not need
    # flash-attn, which keeps the worker off the CUDA runtime.
    qwen_tts_device: str = "cuda:0"
    qwen_tts_attn_implementation: str = "sdpa"
    # Audible AI disclosure (EU AI Act Art. 50(4)). Empty text disables it even
    # when the collection flag is on, which is the escape hatch if a legal
    # review concludes the duty does not apply.
    narration_ai_disclosure_text: str = (
        "Este episodio se ha producido con una voz generada por inteligencia artificial."
    )
    narration_ai_disclosure_interval_minutes: int = Field(default=0, ge=0, le=60)
    # Hard budget caps. The estimated daily spend is ~$0.10, so the defaults
    # sit 10x above it: they never cut normal operation but do stop a runaway
    # retry loop, which is what they are for.
    modal_budget_usd_daily: float = Field(default=1.0, gt=0.0)
    modal_budget_usd_monthly: float = Field(default=10.0, gt=0.0)
    # Anti-retry-storm: minimum seconds between two Modal calls.
    modal_cooldown_minutes: int = Field(default=30, ge=0, le=1440)
    # One Modal container at a time, so a spawn can never double the spend.
    modal_max_concurrency: int = Field(default=1, ge=1, le=4)
    # Runtime caps inside the Modal function, independent of the text length.
    modal_max_audio_seconds_per_episode: int = Field(default=900, ge=60, le=3600)
    modal_max_chunks_per_episode: int = Field(default=20, ge=1, le=100)
    # L4 on Modal Starter, used only to estimate cost before a call.
    modal_usd_per_gpu_second: float = Field(default=0.000222, gt=0.0)
    # Throughput model for the estimate: measured chars/s and RTF on L4.
    modal_estimated_chars_per_second: float = Field(default=17.5, gt=0.0)
    modal_estimated_rtf: float = Field(default=0.65, gt=0.0, le=10.0)
    modal_budget_alert_percent: list[int] = Field(default_factory=lambda: [50, 80, 100])
    # Runtime voice catalog: profiles are versionable in the repo, generated
    # references and the manifest live here and are never committed.
    voices_dir: str = "data/voices"

    @model_validator(mode="after")
    def _conversation_block_bounds(self) -> "Settings":
        """Keep the block merge floor at or below the block turn cap."""
        if (
            self.narration_conversation_min_block_turns
            > self.narration_conversation_max_turns_per_block
        ):
            raise ValueError(
                "narration_conversation_min_block_turns must not exceed "
                "narration_conversation_max_turns_per_block"
            )
        return self

    @property
    def data_dir(self) -> Path:
        """Return the data directory (parent of database file)."""
        return Path(self.database_path).parent

    @property
    def narration_base_url(self) -> str:
        """Return the narration gateway base URL, falling back to the TTS one.

        Returns:
            `narration_api_base_url` when set, otherwise `tts_api_base_url`.
        """
        return self.narration_api_base_url or self.tts_api_base_url

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return settings
