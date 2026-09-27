"""Temporal workflow definitions for the news-engine pipeline.

Workflows orchestrate the daily digest pipeline. They must be deterministic:
no network I/O, no randomness, no external process calls. All side effects
live in activities.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from src.api.models.monitoring import MonitorReport, MonitorRunConfig
    from src.api.services.monitoring.reporter import format_alert_message
    from src.workers.activities import (
        cluster_activity,
        collections_changes_activity,
        compress_audio_activity,
        deliver_email_activity,
        deliver_logic_activity,
        deliver_telegram_audio_activity,
        deliver_telegram_message_activity,
        deliver_whatsapp_audio_activity,
        deliver_whatsapp_message_activity,
        fetch_rss_activity,
        filter_new_activity,
        generate_digest_activity,
        generate_logic_activity,
        generate_tts_activity,
        get_monitor_state_activity,
        get_synced_signature_activity,
        read_collections_signature_activity,
        record_run_activity,
        run_monitor_checks_activity,
        send_monitor_alert_activity,
        set_monitor_state_activity,
        sync_collections_schedules_activity,
    )

# Reasonable per-step timeouts for activity execution.
RSS_FETCH_TIMEOUT = timedelta(seconds=120)
FILTER_TIMEOUT = timedelta(seconds=30)
CLUSTER_TIMEOUT = timedelta(seconds=1800)
DIGEST_TIMEOUT = timedelta(seconds=1800)
# The separate selection-logic LLM call can take as long as the digest itself;
# the follow-up delivery spans the telegram/email/whatsapp channels.
LOGIC_TIMEOUT = DIGEST_TIMEOUT
LOGIC_DELIVERY_TIMEOUT = timedelta(seconds=360)
# Timeouts for the collections-config monitor activities: reading the file and
# the KV lookups are local, while re-registering schedules talks to Temporal.
COLLECTIONS_SIGNATURE_TIMEOUT = timedelta(seconds=30)
COLLECTIONS_SYNC_TIMEOUT = timedelta(seconds=120)
# Timeout for a single TTS activity. The OmniVoice cloned-voice backend runs at
# ~0.9 s/char (a 400-char chunk took ~345s); a 5000-char digest = ~13 chunks x
# up to tts_request_timeout(600s) = ~130 min worst case, so keep this above
# 13 x 600s while the real expected duration is ~75 min.
TTS_TIMEOUT = timedelta(seconds=8100)
# Local ffmpeg transcode of the synthesized WAV; fast even for long digests.
COMPRESS_TIMEOUT = timedelta(seconds=600)
DELIVERY_TIMEOUT = timedelta(seconds=60)
EMAIL_TIMEOUT = timedelta(seconds=90)
# WhatsApp pacing (human delay + per-recipient cool-down) per message multiplied
# across recipients/chunks; give it head-room over the Telegram timeout.
WHATSAPP_TIMEOUT = timedelta(seconds=120)
RECORD_RUN_TIMEOUT = timedelta(seconds=30)

# Phase 7 narration. The digest worker does not register the narration
# activities, so they are addressed BY NAME and dispatched to the narration
# task queue via task_queue=...; the name resolves on the narration worker only.
NARRATION_RENDER_ACTIVITY = "render_narration_activity"
RESOLVE_SCRIPT_ACTIVITY = "resolve_narration_script_activity"
# Longest plausible render: the gateway is called once per turn (each bounded by
# narration_request_timeout, default 300s) before the local mix/encode.
NARRATION_RENDER_TIMEOUT = timedelta(seconds=7200)
# Timeout for script resolution (LLM generation + validation).
NARRATION_SCRIPT_TIMEOUT = timedelta(seconds=300)
# A narration failure must never fail the digest: the text already went out.
NARRATION_RETRY_POLICY = RetryPolicy(maximum_attempts=2)

# Timeouts for the system-monitor workflow activities: probing every check is
# bounded, delivering alerts spans the telegram/email/whatsapp channels, and the
# KV lookups are local.
MONITOR_RUN_TIMEOUT = timedelta(seconds=120)
MONITOR_ALERT_TIMEOUT = timedelta(seconds=120)
MONITOR_STATE_TIMEOUT = timedelta(seconds=30)
COLLECTIONS_DIFF_TIMEOUT = timedelta(seconds=30)

# Bounded retries for network-dependent activities so a transient failure does
# not replay forever. Deterministic errors (validation etc.) should be raised as
# non-retryable ApplicationErrors instead of relying on the default policy.
NETWORK_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass
class PipelineConfig:
    """Input configuration for a single digest pipeline run."""

    feed_urls: list[str] = field(default_factory=list)
    tts_enabled: bool = True
    deliver_audio: bool = True
    podcast_name: str = ""
    podcast_behavior: str = ""
    language: str = ""
    voice: str = ""
    collection: str = "default"
    prompt_template: str = "default"
    custom_prompt: str = ""
    chat_id: str | None = None
    email_recipients: list[str] = field(default_factory=list)
    whatsapp_recipients: list[str] = field(default_factory=list)
    whatsapp_group_id: str | None = None
    delivery_channels: list[str] = field(default_factory=lambda: ["telegram"])
    logic_delivery: dict[str, Any] | None = None
    # Phase 7 narration opt-in. The kill switch and the per-collection choice are
    # resolved **outside** the workflow (a workflow cannot read settings), so the
    # dispatcher fills this dict with the payload plus the narration task queue.
    # None means "this collection does not narrate".
    narration: dict[str, Any] | None = None
    # Configurable story counts (Phase 7.6)
    main_story_count: int = 5
    headline_count: int = 5
    # Whether to deliver the narration audio (default true when narration_enabled)
    narration_deliver_audio: bool = True


@workflow.defn
class DigestPipelineWorkflow:
    """Daily digest pipeline: fetch -> dedup -> cluster -> summarize -> deliver."""

    @workflow.run
    async def run(self, config: PipelineConfig) -> dict[str, Any]:
        """Execute the full digest pipeline.

        Args:
            config: PipelineConfig with feed URLs and delivery options.

        Returns:
            Dict summarizing the run outcome (digest, counts, delivery status).
        """
        workflow.logger.info(
            f"Pipeline started for collection '{config.collection}' "
            f"with {len(config.feed_urls)} feeds"
        )

        articles = await workflow.execute_activity(
            fetch_rss_activity,
            config.feed_urls,
            start_to_close_timeout=RSS_FETCH_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        if not isinstance(articles, list):
            raise ApplicationError(
                "fetch_rss_activity returned an unexpected shape",
                non_retryable=True,
            )
        workflow.logger.info(f"RSS fetched: {len(articles)} articles")

        new_articles = await workflow.execute_activity(
            filter_new_activity,
            args=[articles, config.collection],
            start_to_close_timeout=FILTER_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        if not isinstance(new_articles, list):
            raise ApplicationError(
                "filter_new_activity returned an unexpected shape",
                non_retryable=True,
            )
        workflow.logger.info(f"Filtering done: {len(new_articles)} new")

        if not new_articles:
            workflow.logger.info("No new articles; skipping digest")
            await self._record_run(
                status="no_new_articles",
                collection=config.collection,
                article_count=0,
                cluster_count=0,
                digest_preview=None,
                audio_path=None,
                error=None,
            )
            return {"status": "no_new_articles", "article_count": 0}

        clusters = await workflow.execute_activity(
            cluster_activity,
            args=[new_articles, config.collection],
            start_to_close_timeout=CLUSTER_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        if not isinstance(clusters, list):
            raise ApplicationError(
                "cluster_activity returned an unexpected shape",
                non_retryable=True,
            )
        workflow.logger.info(f"Clustering done: {len(clusters)} clusters")

        digest = await workflow.execute_activity(
            generate_digest_activity,
            {
                "clusters": clusters,
                "podcast_name": config.podcast_name,
                "podcast_behavior": config.podcast_behavior,
                "language": config.language,
                "prompt_template": config.prompt_template,
                "custom_prompt": config.custom_prompt or None,
                "collection": config.collection,
                "main_story_count": config.main_story_count,
                "headline_count": config.headline_count,
            },
            start_to_close_timeout=DIGEST_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        digest_markdown = self._extract_digest_text(digest)
        article_count = self._extract_article_count(digest)
        workflow.logger.info(
            f"Digest generated for {article_count} articles (collection '{config.collection}')"
        )

        channels = config.delivery_channels or ["telegram"]
        use_telegram = "telegram" in channels
        use_email = "email" in channels
        use_whatsapp = bool(config.whatsapp_recipients or config.whatsapp_group_id) and (
            "whatsapp" in channels
        )
        delivery_status: list[dict[str, Any]] = []

        # Deliver the text digest FIRST; it is the primary deliverable and must
        # not be blocked by (optional) TTS/audio failures. A delivery failure on
        # one channel (Telegram/email) is recorded and does not abort the run.
        if use_telegram:
            try:
                text_delivery = await workflow.execute_activity(
                    deliver_telegram_message_activity,
                    args=[digest_markdown, config.chat_id],
                    start_to_close_timeout=DELIVERY_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                delivery_status.append(text_delivery)
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(
                    f"Telegram text step failed; continuing with other channels: {detail}"
                )
                delivery_status.append({"status": "error", "error": str(detail)})

        # Generate TTS audio BEFORE the email step so the audio file (when
        # produced) can be attached to the email. A failure here must not fail
        # the pipeline: the text digest already went out and the run continues.
        # When delivery is requested, the WAV is transcoded to a smaller M4A
        # (AAC) file; if ffmpeg fails the original WAV is used as a fallback so
        # audio delivery is never blocked by the compression step.
        audio: dict[str, Any] | None = None
        if config.tts_enabled:
            workflow.logger.info("Generating TTS audio")
            try:
                audio = await workflow.execute_activity(
                    generate_tts_activity,
                    {
                        "digest": digest_markdown,
                        "language": config.language,
                        "voice": config.voice,
                        "run_id": workflow.info().workflow_id,
                        "collection": config.collection,
                    },
                    start_to_close_timeout=TTS_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                if config.deliver_audio and audio.get("audio_path"):
                    try:
                        compressed = await workflow.execute_activity(
                            compress_audio_activity,
                            audio["audio_path"],
                            start_to_close_timeout=COMPRESS_TIMEOUT,
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        )
                        audio["audio_path"] = compressed["audio_path"]
                    except (ActivityError, ApplicationError) as exc:
                        detail = getattr(exc, "cause", None) or exc
                        workflow.logger.warning(
                            f"Audio compression failed; delivering WAV instead: {detail}"
                        )
                elif config.deliver_audio and not audio.get("audio_path"):
                    delivery_status.append({"status": "error", "error": "no audio generated"})
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(
                    f"TTS/audio step failed; text digest already delivered: {detail}"
                )
                delivery_status.append({"status": "error", "error": str(detail)})
                audio = None

        # Deliver the digest via email when the collection enables the email
        # channel and configures recipients. When audio was generated and
        # delivery is requested, the audio file is attached to this email. A
        # failure here must not fail the whole pipeline (the Telegram digest
        # already went out).
        if use_email and config.email_recipients:
            workflow.logger.info("Delivering email digest")
            audio_for_email = audio.get("audio_path") if audio and config.deliver_audio else None
            try:
                email_delivery = await workflow.execute_activity(
                    deliver_email_activity,
                    args=[
                        config.email_recipients,
                        digest_markdown,
                        config.collection,
                        audio_for_email,
                    ],
                    start_to_close_timeout=EMAIL_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                delivery_status.append(email_delivery)
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(
                    f"Email step failed; text digest already delivered: {detail}"
                )
                delivery_status.append({"status": "error", "error": str(detail)})

        # Deliver the digest via WhatsApp when the collection enables the channel
        # and configures recipients or a group. Fail-closed: an unconfigured
        # gateway or a number outside the allowlist is recorded and the rest of
        # the channels continue.
        if use_whatsapp:
            workflow.logger.info("Delivering WhatsApp digest")
            try:
                whatsapp_delivery = await workflow.execute_activity(
                    deliver_whatsapp_message_activity,
                    args=[
                        digest_markdown,
                        config.whatsapp_recipients,
                        config.whatsapp_group_id,
                    ],
                    start_to_close_timeout=WHATSAPP_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                delivery_status.append(whatsapp_delivery)
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(
                    f"WhatsApp step failed; text digest already delivered: {detail}"
                )
                delivery_status.append({"status": "error", "error": str(detail)})

        # Deliver the admin selection-logic text when the collection defines
        # a logic_delivery destination. The LLM explains, in a separate call,
        # the news-selection logic behind the digest, anchored on the clusters
        # and the final digest. A failure here is recorded and does not abort
        # the run nor its other deliveries.
        if config.logic_delivery:
            workflow.logger.info("Generating and delivering selection logic")
            try:
                logic_result = await workflow.execute_activity(
                    generate_logic_activity,
                    {
                        "clusters": clusters,
                        "digest": digest_markdown,
                        "collection": config.collection,
                        "prompt_template": config.prompt_template,
                        "custom_prompt": config.custom_prompt or None,
                        "language": config.language,
                        "main_story_count": config.main_story_count,
                        "headline_count": config.headline_count,
                    },
                    start_to_close_timeout=LOGIC_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                if not isinstance(logic_result, dict) or not logic_result.get("logic"):
                    raise ApplicationError(
                        "generate_logic_activity returned no logic text",
                        non_retryable=True,
                    )
                logic_delivery = await workflow.execute_activity(
                    deliver_logic_activity,
                    args=[logic_result["logic"], config.logic_delivery, config.collection],
                    start_to_close_timeout=LOGIC_DELIVERY_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                if isinstance(logic_delivery, dict) and logic_delivery.get("deliveries"):
                    delivery_status.extend(logic_delivery["deliveries"])
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(f"Selection logic step failed; continuing: {detail}")
                delivery_status.append({"status": "error", "error": str(detail)})

        # Deliver audio to Telegram when the channel is selected, delivery is
        # requested, and the TTS step produced an audio file.
        if use_telegram and config.tts_enabled and config.deliver_audio:
            if audio and audio.get("audio_path"):
                try:
                    audio_delivery = await workflow.execute_activity(
                        deliver_telegram_audio_activity,
                        args=[audio["audio_path"], digest_markdown[:1000], config.chat_id],
                        start_to_close_timeout=DELIVERY_TIMEOUT,
                        retry_policy=NETWORK_RETRY_POLICY,
                    )
                    delivery_status.append(audio_delivery)
                except (ActivityError, ApplicationError) as exc:
                    detail = getattr(exc, "cause", None) or exc
                    workflow.logger.warning(
                        f"Telegram audio step failed; text digest already delivered: {detail}"
                    )
                    delivery_status.append({"status": "error", "error": str(detail)})

        # Deliver audio to WhatsApp when the channel is selected, delivery is
        # requested, and the TTS step produced an audio file.
        if use_whatsapp and config.tts_enabled and config.deliver_audio:
            if audio and audio.get("audio_path"):
                try:
                    whatsapp_audio = await workflow.execute_activity(
                        deliver_whatsapp_audio_activity,
                        args=[
                            audio["audio_path"],
                            digest_markdown[:1000],
                            config.whatsapp_recipients,
                            config.whatsapp_group_id,
                        ],
                        start_to_close_timeout=WHATSAPP_TIMEOUT,
                        retry_policy=NETWORK_RETRY_POLICY,
                    )
                    delivery_status.append(whatsapp_audio)
                except (ActivityError, ApplicationError) as exc:
                    detail = getattr(exc, "cause", None) or exc
                    workflow.logger.warning(
                        f"WhatsApp audio step failed; text digest already delivered: {detail}"
                    )
                    delivery_status.append({"status": "error", "error": str(detail)})

        workflow.logger.info("Pipeline completed")
        audio_path = audio.get("audio_path") if audio else None
        await self._record_run(
            status="ok",
            collection=config.collection,
            article_count=article_count,
            cluster_count=len(clusters),
            digest_preview=digest_markdown,
            audio_path=audio_path,
            error=None,
        )

        # Phase 7 narration: a second, independent audio spoke rendered by the
        # narration worker on its own task queue. The activity is addressed by
        # name because the digest worker does not register the narration module,
        # and task_queue comes from the workflow input (a workflow cannot read
        # settings). Any failure is recorded and never fails the digest: the
        # text digest, the TTS audio and the deliveries are already done. It
        # runs AFTER the run row is recorded because the narration worker writes
        # digest_runs.narration_path, and annotating a row that does not exist
        # yet would silently lose the path.
        if config.narration:
            narration_request = self._narration_payload(config, workflow.info().workflow_id)
            workflow.logger.info(f"Dispatching narration to '{narration_request['task_queue']}'")
            try:
                # Step 1: Resolve the narration script (generates from digest if needed)
                resolved = await workflow.execute_activity(
                    RESOLVE_SCRIPT_ACTIVITY,
                    args=[narration_request["payload"]],
                    task_queue=narration_request["task_queue"],
                    start_to_close_timeout=NARRATION_SCRIPT_TIMEOUT,
                    retry_policy=NARRATION_RETRY_POLICY,
                )
                if not isinstance(resolved, dict) or not resolved.get("script"):
                    raise ApplicationError(
                        "resolve_narration_script_activity returned no script",
                        non_retryable=True,
                    )
                # Step 2: Render the resolved script
                render_payload = {
                    **narration_request["payload"],
                    "script": resolved["script"],
                }
                narration = await workflow.execute_activity(
                    NARRATION_RENDER_ACTIVITY,
                    args=[render_payload],
                    task_queue=narration_request["task_queue"],
                    start_to_close_timeout=NARRATION_RENDER_TIMEOUT,
                    retry_policy=NARRATION_RETRY_POLICY,
                )
                narration_audio_path = (
                    narration.get("audio_path") if isinstance(narration, dict) else None
                )
                delivery_status.append(
                    {
                        "channel": "narration",
                        "status": "ok",
                        "audio_path": narration_audio_path,
                    }
                )
                # Deliver narration audio to configured channels if enabled
                if narration_audio_path and config.narration_deliver_audio:
                    await self._deliver_narration_audio(
                        config, narration_audio_path, digest_markdown, delivery_status
                    )
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(f"Narration step failed; digest continues: {detail}")
                delivery_status.append(
                    {"channel": "narration", "status": "error", "error": str(detail)}
                )
                # Send error notification to configured channels
                await self._send_narration_error_alert(config, str(detail), delivery_status)

        return {
            "status": "ok",
            "article_count": article_count,
            "cluster_count": len(clusters),
            "digest": digest_markdown,
            "delivery_status": delivery_status,
        }

    @staticmethod
    def _narration_payload(config: PipelineConfig, run_id: str) -> dict[str, Any]:
        """Build the narration request and its target queue.

        Args:
            config: PipelineConfig carrying the ``narration`` opt-in dict.
            run_id: Id of this digest run, used to key the narration audio job
                and the ``digest_runs.narration_path`` column.

        Returns:
            Dict with the ``task_queue`` to dispatch to and the activity
            ``payload``.

        Raises:
            ApplicationError: If the opt-in dict lacks a task queue or the
                spec, which would fail on the narration worker anyway.
        """
        request = config.narration or {}
        task_queue = request.get("task_queue")
        spec = request.get("spec")
        if not task_queue or not isinstance(spec, dict):
            raise ApplicationError(
                "PipelineConfig.narration needs a 'task_queue' and a 'spec'",
                non_retryable=True,
            )
        return {
            "task_queue": str(task_queue),
            "payload": {
                "run_id": request.get("run_id") or run_id,
                "collection": config.collection,
                "spec": spec,
                "script": request.get("script"),
                "script_id": request.get("script_id"),
                "strategy": request.get("strategy") or "turns",
                "main_story_count": config.main_story_count,
                "headline_count": config.headline_count,
                "prompt_template": config.prompt_template,
            },
        }

    async def _deliver_narration_audio(
        self,
        config: PipelineConfig,
        audio_path: str,
        digest_markdown: str,
        delivery_status: list[dict[str, Any]],
    ) -> None:
        """Deliver the narration audio to all configured channels.

        Args:
            config: PipelineConfig with delivery settings.
            audio_path: Path to the generated narration M4A file.
            digest_markdown: The digest text for captions.
            delivery_status: List to append delivery results to.
        """
        channels = config.delivery_channels or ["telegram"]
        caption = f"{config.podcast_name or config.collection} — Narration".strip()

        if "telegram" in channels:
            try:
                result = await workflow.execute_activity(
                    deliver_telegram_audio_activity,
                    args=[audio_path, caption, config.chat_id],
                    start_to_close_timeout=DELIVERY_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                delivery_status.append({"channel": "telegram_audio", "status": "ok", **result})
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(f"Telegram narration audio failed: {detail}")
                delivery_status.append(
                    {"channel": "telegram_audio", "status": "error", "error": str(detail)}
                )

        if "email" in channels and config.email_recipients:
            try:
                result = await workflow.execute_activity(
                    deliver_email_activity,
                    args=[config.email_recipients, digest_markdown, config.collection, audio_path],
                    start_to_close_timeout=EMAIL_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                delivery_status.append({"channel": "email_narration", "status": "ok", **result})
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(f"Email narration audio failed: {detail}")
                delivery_status.append(
                    {"channel": "email_narration", "status": "error", "error": str(detail)}
                )

        if "whatsapp" in channels and (config.whatsapp_recipients or config.whatsapp_group_id):
            try:
                result = await workflow.execute_activity(
                    deliver_whatsapp_audio_activity,
                    args=[
                        audio_path,
                        caption,
                        config.whatsapp_recipients,
                        config.whatsapp_group_id,
                    ],
                    start_to_close_timeout=WHATSAPP_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                delivery_status.append({"channel": "whatsapp_narration", "status": "ok", **result})
            except (ActivityError, ApplicationError) as exc:
                detail = getattr(exc, "cause", None) or exc
                workflow.logger.warning(f"WhatsApp narration audio failed: {detail}")
                delivery_status.append(
                    {"channel": "whatsapp_narration", "status": "error", "error": str(detail)}
                )

    async def _send_narration_error_alert(
        self,
        config: PipelineConfig,
        error: str,
        delivery_status: list[dict[str, Any]],
    ) -> None:
        """Send a narration failure alert to all configured channels.

        Args:
            config: PipelineConfig with delivery settings.
            error: The error message from the narration failure.
            delivery_status: List to append delivery results to.
        """
        channels = config.delivery_channels or ["telegram"]
        alert_text = f"⚠️ Narration failed for collection '{config.collection}':\n{error}"

        if "telegram" in channels:
            try:
                await workflow.execute_activity(
                    deliver_telegram_message_activity,
                    args=[alert_text, config.chat_id],
                    start_to_close_timeout=DELIVERY_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
            except (ActivityError, ApplicationError):
                pass  # Best effort

        if "email" in channels and config.email_recipients:
            try:
                await workflow.execute_activity(
                    deliver_email_activity,
                    args=[config.email_recipients, alert_text, config.collection, None],
                    start_to_close_timeout=EMAIL_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
            except (ActivityError, ApplicationError):
                pass  # Best effort

        if "whatsapp" in channels and (config.whatsapp_recipients or config.whatsapp_group_id):
            try:
                await workflow.execute_activity(
                    deliver_whatsapp_message_activity,
                    args=[alert_text, config.whatsapp_recipients, config.whatsapp_group_id],
                    start_to_close_timeout=WHATSAPP_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
            except (ActivityError, ApplicationError):
                pass  # Best effort

    async def _record_run(
        self,
        status: str,
        collection: str,
        article_count: int,
        cluster_count: int,
        digest_preview: str | None,
        audio_path: str | None,
        error: str | None,
    ) -> None:
        """Persist a summary of this run via the record_run_activity.

        Args:
            status: Run status ('ok', 'no_new_articles', 'failed').
            collection: Collection name the run belongs to.
            article_count: Number of summarized articles.
            cluster_count: Number of story clusters.
            digest_preview: First chars of the digest text.
            audio_path: Generated audio path, if any.
            error: Error message for failed runs (None for successful ones).
        """
        await workflow.execute_activity(
            record_run_activity,
            {
                "workflow_id": workflow.info().workflow_id,
                "collection": collection,
                "status": status,
                "article_count": article_count,
                "cluster_count": cluster_count,
                "digest_preview": digest_preview,
                "audio_path": audio_path,
                "error": error,
            },
            start_to_close_timeout=RECORD_RUN_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )

    @staticmethod
    def _extract_digest_text(digest: Any) -> str:
        """Return the digest markdown, failing fast on a malformed result.

        Args:
            digest: Dict produced by generate_digest_activity.

        Returns:
            The digest markdown string.

        Raises:
            ApplicationError: If the result is missing the digest text.
        """
        if not isinstance(digest, dict) or not digest.get("digest"):
            raise ApplicationError(
                "generate_digest_activity returned no digest text",
                non_retryable=True,
            )
        return str(digest["digest"])

    @staticmethod
    def _extract_article_count(digest: Any) -> int:
        """Return the article count from a digest result.

        Args:
            digest: Dict produced by generate_digest_activity.

        Returns:
            The number of articles summarized.

        Raises:
            ApplicationError: If the result has no article count.
        """
        if not isinstance(digest, dict) or "article_count" not in digest:
            raise ApplicationError(
                "generate_digest_activity returned no article_count",
                non_retryable=True,
            )
        return int(digest["article_count"])


@workflow.defn
class CollectionsMonitorWorkflow:
    """Keep the Temporal schedules in sync with collections.toml.

    Runs on a fixed schedule (see ``schedule.monitor_schedule``). Each run
    compares the sha256 of the current collections file against the signature
    stored after the last registration; when they differ, it re-registers the
    per-collection schedules so a feed/time/voice change in the TOML takes
    effect without a manual ``python -m src.workers.schedule`` call. When the
    change added or removed collections, an alert is delivered through the
    monitor channels.
    """

    @workflow.run
    async def run(self, unused: None = None) -> dict[str, Any]:
        """Check for and apply schedule drift on each schedule tick.

        Args:
            unused: No input expected from the schedule action.

        Returns:
            Dict describing the run outcome ('synced', 'unchanged', or 'skipped').
        """
        signature = await workflow.execute_activity(
            read_collections_signature_activity,
            start_to_close_timeout=COLLECTIONS_SIGNATURE_TIMEOUT,
        )
        if not isinstance(signature, str):
            workflow.logger.info("collections_file_unavailable; skipping sync")
            return {"status": "skipped", "changed": False}

        last_signature = await workflow.execute_activity(
            get_synced_signature_activity,
            start_to_close_timeout=COLLECTIONS_SIGNATURE_TIMEOUT,
        )
        if last_signature == signature:
            workflow.logger.info("collections_config_unchanged")
            return {"status": "unchanged", "changed": False}

        workflow.logger.info("collections_config_changed; computing diff")
        changes = await workflow.execute_activity(
            collections_changes_activity,
            start_to_close_timeout=COLLECTIONS_DIFF_TIMEOUT,
        )
        added = tuple(changes.get("added", [])) if isinstance(changes, dict) else ()
        removed = tuple(changes.get("removed", [])) if isinstance(changes, dict) else ()

        workflow.logger.info("collections_config_changed; re-registering schedules")
        await workflow.execute_activity(
            sync_collections_schedules_activity,
            args=[signature],
            start_to_close_timeout=COLLECTIONS_SYNC_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )

        if added or removed:
            lines = ["Collections configuration updated", ""]
            if added:
                lines.append(f"Added: {', '.join(added)}")
            if removed:
                lines.append(f"Removed: {', '.join(removed)}")
            await self._send_alert("\n".join(lines))

        return {"status": "synced", "changed": True, "signature": signature[:12]}

    async def _send_alert(self, text: str) -> None:
        """Deliver a message through the monitor channels without failing.

        Args:
            text: The message body to send.
        """
        try:
            await workflow.execute_activity(
                send_monitor_alert_activity,
                args=[text],
                start_to_close_timeout=MONITOR_ALERT_TIMEOUT,
                retry_policy=NETWORK_RETRY_POLICY,
            )
        except (ActivityError, ApplicationError) as exc:
            detail = getattr(exc, "cause", None) or exc
            workflow.logger.warning(f"Monitor alert delivery failed; continuing: {detail}")


@workflow.defn
class SystemMonitorWorkflow:
    """Run the system-health checks on the configured interval.

    Each tick runs every registered check (TTS, collections, processes, disk,
    digest runs), persists the outcomes to SQLite + JSONL logs, alerts when a
    check transitions to error/warning, and optionally sends a once-per-UTC-day
    full health summary.
    """

    @workflow.run
    async def run(self, config: MonitorRunConfig) -> dict[str, Any]:
        """Collect, persist, and deliver the monitoring report.

        Args:
            config: MonitorRunConfig with daily_summary / alert_on_run flags.

        Returns:
            Dict summarizing the run (status, check and alert counts).
        """
        workflow.logger.info("monitor_run_started")
        report = await workflow.execute_activity(
            run_monitor_checks_activity,
            args=[config.model_dump()],
            start_to_close_timeout=MONITOR_RUN_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        if not isinstance(report, dict):
            workflow.logger.info("monitor_run_unexpected_shape")
            return {"status": "error", "error": "unexpected report shape"}

        summary_sent = False
        if report.get("alert"):
            await self._send(format_alert_message(MonitorReport(**report)))

        if config.daily_summary:
            today = workflow.now().strftime("%Y-%m-%d")
            last_day = await workflow.execute_activity(
                get_monitor_state_activity,
                args=["monitor:last_summary_day"],
                start_to_close_timeout=MONITOR_STATE_TIMEOUT,
            )
            if last_day != today:
                await self._send(format_alert_message(MonitorReport(**report), is_summary=True))
                await workflow.execute_activity(
                    set_monitor_state_activity,
                    args=["monitor:last_summary_day", today],
                    start_to_close_timeout=MONITOR_STATE_TIMEOUT,
                )
                summary_sent = True

        workflow.logger.info(
            f"monitor_run_finished checks={len(report.get('checks', []))} "
            f"alerts={len(report.get('alert', []))}"
        )
        return {
            "status": "ok",
            "checks": len(report.get("checks", [])),
            "alerts": len(report.get("alert", [])),
            "summary_sent": summary_sent,
        }

    async def _send(self, text: str) -> None:
        """Deliver a message through the monitor channels without failing.

        Args:
            text: The message body to send.
        """
        try:
            await workflow.execute_activity(
                send_monitor_alert_activity,
                args=[text],
                start_to_close_timeout=MONITOR_ALERT_TIMEOUT,
                retry_policy=NETWORK_RETRY_POLICY,
            )
        except (ActivityError, ApplicationError) as exc:
            detail = getattr(exc, "cause", None) or exc
            workflow.logger.warning(f"Monitor alert delivery failed; continuing: {detail}")
