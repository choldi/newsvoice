"""Temporal workflow for the multi-speaker narration pipeline (Phase 7).

The narration spoke has its own workflow so a narration run is a first-class,
separately retryable execution in the Temporal UI, and so it can be started
standalone (manual trigger, admin API) with or without a digest run behind it.

The activities are addressed **by name** with no ``task_queue`` override: they
run on the very queue this workflow was scheduled on, which is served by the
narration worker. See ``src/workers/narration_activities.py``.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

# Resolving a script only reads the database and validates the models.
NARRATION_SCRIPT_TIMEOUT = timedelta(seconds=60)
# The render is the expensive step: the gateway is called once per turn (each
# bounded by narration_request_timeout, default 300s) and the result is mixed
# and encoded locally. A 20k-character program is ~14 requests, so keep the
# ceiling well above that; retries are cheap because identical renders hit the
# content-hash cache.
NARRATION_RENDER_TIMEOUT = timedelta(seconds=7200)
# Recording the audio job row and the run path is local SQLite work.
NARRATION_RECORD_TIMEOUT = timedelta(seconds=30)

#: Retries for the render: the service already falls back to Edge when the
#: gateway is unreachable, so a second attempt is only worth it for a transient
#: local failure, not for a systematic one.
NARRATION_RENDER_RETRY_POLICY = RetryPolicy(maximum_attempts=2)
LOCAL_RETRY_POLICY = RetryPolicy(maximum_attempts=3)

#: Activity names, kept as constants because they are resolved by string.
RESOLVE_SCRIPT_ACTIVITY = "resolve_narration_script_activity"
RENDER_NARRATION_ACTIVITY = "render_narration_activity"


@dataclass
class NarrationConfig:
    """Input configuration for a single narration run.

    Attributes:
        collection: Collection the narration belongs to.
        spec: NarrationSpec payload (cast, pauses, music bed).
        script: NarrationScript payload, when the caller already has it.
        script_id: Id of a script stored via the admin API, used when
            ``script`` is not set.
        run_id: Digest run this narration belongs to. When empty a synthetic id
            derived from the workflow id is used, so standalone runs still get
            their own audio_jobs row.
    """

    collection: str = "default"
    spec: dict[str, Any] = field(default_factory=dict)
    script: dict[str, Any] | None = None
    script_id: int | None = None
    run_id: str = ""
    strategy: str = "turns"


@workflow.defn
class NarrationWorkflow:
    """Render one multi-speaker narration: resolve script -> render -> attach."""

    @workflow.run
    async def run(self, config: NarrationConfig) -> dict[str, Any]:
        """Execute one narration run.

        Args:
            config: NarrationConfig with the cast, the script (or its id) and
                the render strategy.

        Returns:
            Dict describing the render, plus the resolved script metadata.

        Raises:
            ApplicationError: If no script can be resolved, or the render
                activity fails after its retries.
        """
        workflow.logger.info(
            f"Narration started for collection '{config.collection}' (strategy '{config.strategy}')"
        )
        if not config.spec:
            raise ApplicationError("NarrationWorkflow needs a 'spec' payload", non_retryable=True)

        resolved = await workflow.execute_activity(
            RESOLVE_SCRIPT_ACTIVITY,
            {
                "spec": config.spec,
                "script": config.script,
                "script_id": config.script_id,
                "collection": config.collection,
            },
            start_to_close_timeout=NARRATION_SCRIPT_TIMEOUT,
            retry_policy=LOCAL_RETRY_POLICY,
        )
        if not isinstance(resolved, dict) or not resolved.get("script"):
            raise ApplicationError(
                "resolve_narration_script_activity returned no script",
                non_retryable=True,
            )
        script = resolved["script"]

        render = await workflow.execute_activity(
            RENDER_NARRATION_ACTIVITY,
            {
                "run_id": config.run_id or workflow.info().workflow_id,
                "collection": config.collection,
                "spec": config.spec,
                "script": script,
                "strategy": config.strategy,
            },
            start_to_close_timeout=NARRATION_RENDER_TIMEOUT,
            retry_policy=NARRATION_RENDER_RETRY_POLICY,
        )
        if not isinstance(render, dict) or not render.get("audio_path"):
            raise ApplicationError(
                "render_narration_activity returned no audio",
                non_retryable=True,
            )
        workflow.logger.info(
            f"Narration completed: {render.get('audio_path')} ({render.get('duration_seconds')}s)"
        )
        return {
            "status": "ok",
            "collection": config.collection,
            "run_id": config.run_id or workflow.info().workflow_id,
            "script_source": resolved.get("source"),
            "segments": resolved.get("segments"),
            "char_count": resolved.get("char_count"),
            "narration": render,
        }
