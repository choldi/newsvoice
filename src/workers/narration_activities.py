"""Temporal activities for the multi-speaker narration pipeline (Phase 7).

These activities are registered **only** on the narration worker (see
``src/workers/run_narration_worker.py``), which polls its own task queue, and
they are addressed **by name** from the workflows: a worker only knows the
activities of its own module, so importing or referencing the digest activities
here (or the narration ones from the digest worker) would fail at runtime with a
NotFoundError.

Everything with side effects lives in these activities: the workflows only
orchestrate.
"""

import time
from typing import Any

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.db.database import AudioJobsRepository, NarrationRepository, RunsRepository
from src.api.models.narration import NarrationScript, NarrationSpec
from src.api.services.narration_tts import (
    NarrationError,
    NarrationTTSService,
    RenderStrategy,
)

#: ``audio_jobs.kind`` discriminator owned by this module. The digest TTS job
#: keeps ``'tts'`` so the two audio paths never overwrite each other.
NARRATION_JOB_KIND = "narration"


def _validated_pair(payload: dict[str, Any]) -> tuple[NarrationSpec, NarrationScript]:
    """Validate the spec/script pair carried by an activity payload.

    Args:
        payload: Activity payload with a ``spec`` dict and a ``script`` dict.

    Returns:
        Tuple of ``(NarrationSpec, NarrationScript)``.

    Raises:
        ApplicationError: If the payload is malformed or the script references
            roles the spec does not define. Non-retryable: replaying the same
            payload cannot fix it.
    """
    raw_spec = payload.get("spec")
    raw_script = payload.get("script")
    if not isinstance(raw_spec, dict) or not isinstance(raw_script, dict):
        raise ApplicationError(
            "Narration activity needs both 'spec' and 'script' dicts",
            non_retryable=True,
        )
    try:
        spec = NarrationSpec.model_validate(raw_spec)
        script = NarrationScript.model_validate(raw_script, context={"spec": spec})
    except ValidationError as exc:
        raise ApplicationError(f"Invalid narration spec/script: {exc}", non_retryable=True) from exc
    return spec, script


def _run_id(payload: dict[str, Any]) -> str:
    """Return the run id an activity payload targets.

    Args:
        payload: Activity payload.

    Returns:
        The explicit ``run_id`` when present, else the calling workflow id.
    """
    return str(payload.get("run_id") or activity.info().workflow_id)


@activity.defn
async def resolve_narration_script_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve the script a narration run will render.

    The script is either carried inline by the caller or already stored by the
    admin API and referenced by id. Validating it against the spec here means a
    role typo fails fast, before the (expensive) render activity runs.

    Args:
        payload: Dict with ``spec``, plus ``script`` (inline script) or
            ``script_id`` (row id in ``narration_scripts``).

    Returns:
        Dict with the validated ``script``, its ``source`` and ``title``.

    Raises:
        ApplicationError: If no script is supplied, the stored id does not
            exist, or the script is invalid for the spec. All non-retryable.
    """
    raw_spec = payload.get("spec")
    if not isinstance(raw_spec, dict):
        raise ApplicationError("Narration activity needs a 'spec' dict", non_retryable=True)
    try:
        spec = NarrationSpec.model_validate(raw_spec)
    except ValidationError as exc:
        raise ApplicationError(f"Invalid narration spec: {exc}", non_retryable=True) from exc

    inline = payload.get("script")
    stored: NarrationScript | None = None
    if not isinstance(inline, dict):
        script_id = payload.get("script_id")
        if not isinstance(script_id, int):
            raise ApplicationError(
                "Narration needs an inline 'script' or an int 'script_id'",
                non_retryable=True,
            )
        stored = await NarrationRepository().get_script(script_id)
        if stored is None:
            raise ApplicationError(f"Narration script {script_id} not found", non_retryable=True)

    try:
        script = NarrationScript.model_validate(inline or stored, context={"spec": spec})
    except ValidationError as exc:
        raise ApplicationError(f"Invalid narration script: {exc}", non_retryable=True) from exc
    return {
        "script": script.model_dump(),
        "source": script.source,
        "title": script.title,
        "segments": len(script.segments),
        "char_count": script.total_chars(),
    }


@activity.defn
async def render_narration_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Render a narration script into a cached M4A and attach it to the run.

    Progress is recorded in ``audio_jobs`` with ``kind='narration'`` so clients
    can poll the narration status without touching the digest TTS job, and the
    resulting file is written to ``digest_runs.narration_path``.

    Args:
        payload: Dict with ``spec``, ``script``, optional ``run_id``,
            ``collection`` and ``strategy`` ('turns' or 'conversations').

    Returns:
        Dict describing the render (audio_path, wav_path, duration_seconds,
        cached, voices, music, edge_fallbacks, ...).

    Raises:
        ApplicationError: If the payload is invalid (non-retryable) or the
            render fails.
    """
    spec, script = _validated_pair(payload)
    run_id = _run_id(payload)
    collection = str(payload.get("collection") or "default")
    raw_strategy = str(payload.get("strategy") or RenderStrategy.TURNS)
    try:
        strategy = RenderStrategy(raw_strategy)
    except ValueError as exc:
        raise ApplicationError(
            f"Unknown render strategy: {raw_strategy}", non_retryable=True
        ) from exc

    activity.logger.info(
        "Rendering narration",
        collection=collection,
        run_id=run_id,
        segments=len(script.segments),
        strategy=str(strategy),
    )
    jobs = AudioJobsRepository()
    await jobs.start_job(run_id, collection, kind=NARRATION_JOB_KIND)
    await jobs.set_generating(run_id, kind=NARRATION_JOB_KIND)
    started = time.monotonic()
    service = NarrationTTSService()
    try:
        result = await service.render(spec, script, collection=collection, strategy=strategy)
    except Exception as exc:
        await jobs.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
            kind=NARRATION_JOB_KIND,
        )
        # Validation problems (unknown role, bad voice) can never succeed on a
        # replay, so they are not retried; anything else keeps the default
        # retryable behaviour and benefits from the content-hash cache.
        raise ApplicationError(
            f"Narration render failed: {exc}",
            non_retryable=isinstance(exc, (ValidationError, NarrationError)),
        ) from exc

    elapsed = round(time.monotonic() - started, 2)
    await jobs.finish_job(
        run_id,
        status="FINISHED_OK",
        audio_path=result.audio_path,
        elapsed_seconds=elapsed,
        kind=NARRATION_JOB_KIND,
    )
    updated = await RunsRepository().set_narration_path(run_id, result.audio_path)
    if not updated:
        # Standalone narration runs have no digest row to annotate; that is not
        # an error, the audio and the job row already exist. The flag lets the
        # caller (and 7.4's API) tell both cases apart.
        activity.logger.info(
            "narration_run_without_digest_row",
            run_id=run_id,
            audio_path=result.audio_path,
        )
    payload = result.model_dump()
    payload["run_attached"] = bool(updated)
    return payload
