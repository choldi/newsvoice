"""Temporal workflow for the voice catalog (Phase 7.8).

Generates every missing or stale reference voice in the catalog, one activity
per profile. A profile that fails does not abort the batch: each result is
recorded with its own status so the operator can retry the failures.

The workflow does no I/O: the catalog is read by ``voice_catalog_status_activity``
and every side effect lives in the design/upload activities.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

#: VoiceDesign loads a 1.7B model and synthesises one short sample; 10 min is
#: generous for a cold start plus generation.
DESIGN_VOICE_TIMEOUT = timedelta(minutes=10)
#: Uploading a reference WAV to the Volume is a single file write.
UPLOAD_VOICE_TIMEOUT = timedelta(minutes=2)
#: Status reads the catalog and the manifest from local disk.
STATUS_TIMEOUT = timedelta(seconds=30)

#: A design failure is worth one retry: a Modal cold start can time out.
DESIGN_VOICE_RETRY_POLICY = RetryPolicy(maximum_attempts=2)

#: Activity names, kept as constants because they are resolved by string.
VOICE_STATUS_ACTIVITY = "voice_catalog_status_activity"
DESIGN_VOICE_ACTIVITY = "design_voice_activity"
UPLOAD_VOICE_ACTIVITY = "upload_voice_activity"


@dataclass
class VoiceCatalogConfig:
    """Input configuration for a voice catalog run.

    Attributes:
        names: Optional subset of profile names to (re)generate. Empty means
            every enabled profile that is missing or stale.
        upload: Also upload each generated reference to the Modal Volume.
        task_queue: Modal narration queue the activities run on. A workflow
            cannot read settings, so the caller supplies it.
    """

    names: list[str] = field(default_factory=list)
    upload: bool = False
    task_queue: str = ""


@workflow.defn
class VoiceCatalogWorkflow:
    """Generate the reference voices the catalog is missing."""

    @workflow.run
    async def run(self, config: VoiceCatalogConfig) -> dict[str, Any]:
        """Execute the voice catalog run.

        Args:
            config: VoiceCatalogConfig with the optional name subset and the
                upload flag.

        Returns:
            Dict with ``pending`` and per-profile ``results``.
        """
        status = await workflow.execute_activity(
            VOICE_STATUS_ACTIVITY,
            {"names": config.names},
            task_queue=config.task_queue,
            start_to_close_timeout=STATUS_TIMEOUT,
        )
        pending = [str(name) for name in (status.get("pending") or [])]
        profiles = {str(p["name"]): p for p in (status.get("profiles") or [])}
        workflow.logger.info(f"Voice catalog run: {len(pending)} profiles pending")

        results: list[dict[str, Any]] = []
        for name in pending:
            profile = profiles[name]
            try:
                designed = await workflow.execute_activity(
                    DESIGN_VOICE_ACTIVITY,
                    {
                        "name": profile["name"],
                        "instruct": profile["instruct"],
                        "ref_text": profile["ref_text"],
                        "language": profile["language"],
                    },
                    task_queue=config.task_queue,
                    start_to_close_timeout=DESIGN_VOICE_TIMEOUT,
                    retry_policy=DESIGN_VOICE_RETRY_POLICY,
                )
                if config.upload:
                    await workflow.execute_activity(
                        UPLOAD_VOICE_ACTIVITY,
                        {"name": profile["name"]},
                        task_queue=config.task_queue,
                        start_to_close_timeout=UPLOAD_VOICE_TIMEOUT,
                    )
                results.append({"name": name, "status": "ok", "voice": designed})
            except Exception as exc:
                workflow.logger.warning(f"Voice design failed for '{name}': {exc}")
                results.append({"name": name, "status": "error", "error": str(exc)})

        return {"pending": pending, "results": results}
