"""Temporal workflow for the voice catalog (Phase 7.8/7.9).

Generates every missing or stale reference voice in the catalog, one activity
per profile. A profile that fails does not abort the batch: each result is
recorded with its own status so the operator can retry the failures.

The workflow does no I/O and runs on the narration queue: the catalog is read
by ``voice_catalog_status_activity``, the GPU box does the synthesis on its own
queue, and every side effect lives in the design/store/upload activities.

Reference audio never travels through this workflow: the design activity stages
it on the server and the store activity reads it back, so a whole 24-voice
batch adds only digests to the history. What the workflow does carry, it keeps
short — hence the single-attempt upload policy below.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

#: VoiceDesign loads a 1.7B model and synthesises one short sample. The local
#: worker finishes in well under a minute, but the Modal branch (Phase 10.4)
#: also pays the cold start — image pull to a fresh L4 container plus model
#: load: a healthy remote run measured ~4.5 min and the first worker-side run
#: blew past 10 min, so the budget matches the function's own 1800 s timeout.
DESIGN_VOICE_TIMEOUT = timedelta(minutes=30)
#: Storing a reference is a local file write plus a manifest update.
STORE_VOICE_TIMEOUT = timedelta(minutes=2)
#: Uploading a reference WAV to the Volume is a single file write.
UPLOAD_VOICE_TIMEOUT = timedelta(minutes=2)
#: Status reads the catalog and the manifest from local disk.
STATUS_TIMEOUT = timedelta(seconds=30)

#: A design failure is worth one retry: a cold start can time out.
DESIGN_VOICE_RETRY_POLICY = RetryPolicy(maximum_attempts=2)
#: Storing only fails on a full disk or a corrupt payload, neither of which
#: a couple of retries can fix, but a transient I/O error is worth one more go.
STORE_VOICE_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
#: An upload failure is configuration, not flakiness: missing Modal credentials,
#: a missing ``modal`` package, a bad token. The default policy retries it
#: forever with backoff, which is how the first catalog run wedged a workflow
#: for hours while doing nothing but waiting. One attempt, then the profile is
#: marked failed and the operator fixes the configuration.
UPLOAD_VOICE_RETRY_POLICY = RetryPolicy(maximum_attempts=1)

#: Activity names, kept as constants because they are resolved by string.
VOICE_STATUS_ACTIVITY = "voice_catalog_status_activity"
DESIGN_VOICE_ACTIVITY = "design_voice_activity"
STORE_VOICE_ACTIVITY = "store_voice_activity"
UPLOAD_VOICE_ACTIVITY = "upload_voice_activity"


@dataclass
class VoiceCatalogConfig:
    """Input configuration for a voice catalog run.

    Attributes:
        names: Optional subset of profile names to (re)generate. Empty means
            every enabled profile that is missing or stale.
        upload: Also upload each generated reference to the Modal Volume.
        run_in_modal: Render every design on Modal instead of the local GPU
            worker (Phase 10.4 switch; refused when Modal is disabled).
        task_queue: Narration queue the status, store and upload activities
            run on. A workflow cannot read settings, so the caller supplies it.
        design_task_queue: Voice-design queue polled by the GPU box. Falls
            back to ``task_queue`` when empty, which keeps a single-queue run
            working.
    """

    names: list[str] = field(default_factory=list)
    upload: bool = False
    run_in_modal: bool = False
    task_queue: str = ""
    design_task_queue: str = ""


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
        design_queue = config.design_task_queue or config.task_queue
        workflow.logger.info(f"Voice catalog run: {len(pending)} profiles pending")

        results: list[dict[str, Any]] = []
        for name in pending:
            profile = profiles[name]
            try:
                designed = await workflow.execute_activity(
                    DESIGN_VOICE_ACTIVITY,
                    self._design_payload(profile, config.run_in_modal),
                    task_queue=design_queue,
                    start_to_close_timeout=DESIGN_VOICE_TIMEOUT,
                    retry_policy=DESIGN_VOICE_RETRY_POLICY,
                )
                stored = await workflow.execute_activity(
                    STORE_VOICE_ACTIVITY,
                    self._store_payload(profile, designed),
                    task_queue=config.task_queue,
                    start_to_close_timeout=STORE_VOICE_TIMEOUT,
                    retry_policy=STORE_VOICE_RETRY_POLICY,
                )
                if config.upload:
                    await workflow.execute_activity(
                        UPLOAD_VOICE_ACTIVITY,
                        {"name": profile["name"], "ref_text": profile["ref_text"]},
                        task_queue=config.task_queue,
                        start_to_close_timeout=UPLOAD_VOICE_TIMEOUT,
                        retry_policy=UPLOAD_VOICE_RETRY_POLICY,
                    )
                results.append({"name": name, "status": "ok", "voice": stored})
            except Exception as exc:
                workflow.logger.warning(f"Voice design failed for '{name}': {exc}")
                results.append({"name": name, "status": "error", "error": str(exc)})

        return {"pending": pending, "results": results}

    @staticmethod
    def _design_payload(profile: dict[str, Any], run_in_modal: bool = False) -> dict[str, Any]:
        """Build the design activity input from a catalog profile.

        Args:
            profile: Profile dict as returned by the status activity.
            run_in_modal: Whether the design must render on Modal (Phase 10.4).

        Returns:
            Payload for ``design_voice_activity``.
        """
        payload = {
            "name": profile["name"],
            "instruct": profile["instruct"],
            "ref_text": profile["ref_text"],
            "language": profile["language"],
            "run_in_modal": run_in_modal,
        }
        segments = profile.get("ref_segments")
        if segments:
            payload["ref_segments"] = list(segments)
        return payload

    @staticmethod
    def _store_payload(profile: dict[str, Any], designed: dict[str, Any]) -> dict[str, Any]:
        """Build the store activity input from a profile and a design result.

        Args:
            profile: Profile dict as returned by the status activity.
            designed: Result of ``design_voice_activity``.

        Returns:
            Payload for ``store_voice_activity``. The audio itself is not part
            of it: the design activity staged it under the profile name, and
            only the digest crosses the history.
        """
        return {
            "name": profile["name"],
            "instruct": profile["instruct"],
            "ref_text": profile["ref_text"],
            "sha256": designed["sha256"],
            "design_model": designed.get("design_model", ""),
        }
