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

from src.api.db.database import (
    AudioJobsRepository,
    ModalSpendRepository,
    NarrationRepository,
    RunsRepository,
    VerificationsRepository,
)
from src.api.models.narration import NarrationScript, NarrationSpec, TTSBackend
from src.api.services.audio_verify import AudioVerifyError, verify_audio
from src.api.services.narration_tts import (
    NarrationError,
    NarrationTTSService,
    RenderStrategy,
)
from src.core.budget import preflight
from src.core.config import settings
from src.core.modal_cost import (
    billing_delta,
    billing_snapshot,
    episode_tags,
    record_billing_delta,
    set_cost_context,
)

#: ``audio_jobs.kind`` discriminator owned by this module. The digest TTS job
#: keeps ``'tts'`` so the two audio paths never overwrite each other.
NARRATION_JOB_KIND = "narration"

#: A whole podcast script is thousands of tokens of JSON, so 4k output tokens
#: is the bare minimum for a local model; going higher keeps it from truncating
#: mid-structure.
_SCRIPT_MAX_TOKENS = 8192
#: Formatting failures (prose around the JSON, truncation, empty answer) are
#: retried inside the activity: a fresh sample usually fixes them, and it is
#: much cheaper than failing the workflow and paying Temporal's backoff.
_SCRIPT_FORMAT_ATTEMPTS = 3


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


async def _enforce_modal_budget(
    service: NarrationTTSService,
    spec: NarrationSpec,
    script: NarrationScript,
    *,
    collection: str,
    run_id: str,
    strategy: RenderStrategy,
) -> None:
    """Run the Modal budget preflight before any ``.spawn()``.

    A cache hit is free and always allowed, so an exhausted budget never blocks
    audio that is already on disk. A blocked render is non-retryable: replaying
    the same payload cannot make room in the budget.

    Args:
        service: Narration service, for the cache probe.
        spec: Narration spec.
        script: Script to render.
        collection: Collection name owning the narration.
        run_id: Run id the activity payload targets.
        strategy: Render strategy.

    Raises:
        ApplicationError: When Modal is disabled, or the budget guard blocks
            the render. Non-retryable in both cases.
    """
    if not settings.modal_enabled:
        raise ApplicationError(
            f"tts_backend {spec.tts_backend} requires modal, but modal is disabled",
            non_retryable=True,
        )
    if spec.tts_backend is TTSBackend.ZONOS2 and not settings.zonos2_enabled:
        raise ApplicationError(
            "tts_backend zonos2 is behind the benchmark gate and not enabled",
            non_retryable=True,
        )
    if spec.tts_backend is TTSBackend.VOXCPM2 and not settings.voxcpm2_enabled:
        raise ApplicationError(
            "tts_backend voxcpm2 is behind the benchmark gate and not enabled",
            non_retryable=True,
        )
    cached = service.cache_probe(spec, script, collection=collection, strategy=strategy)
    if cached is not None:
        await ModalSpendRepository().record(
            collection=collection,
            stage="tts",
            backend=str(spec.tts_backend),
            gpu_seconds=0.0,
            cost_usd=0.0,
            run_id=run_id,
            cache_hit=True,
        )
        return
    decision = await preflight(char_count=script.total_chars(), cache_hit=False)
    if not decision.allowed:
        raise ApplicationError(
            f"modal budget guard blocked the render: {decision.reason}",
            non_retryable=True,
        )


@activity.defn
async def resolve_narration_script_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve the script a narration run will render.

    The script is either carried inline by the caller, already stored by the
    admin API and referenced by id, or generated on-the-fly from the digest
    using the LLM. Validating it against the spec here means a role typo fails
    fast, before the (expensive) render activity runs.

    Args:
        payload: Dict with ``spec``, plus ``script`` (inline script) or
            ``script_id`` (row id in ``narration_scripts``). If neither is
            provided, the activity generates a script from the digest run.

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
        if isinstance(script_id, int):
            stored = await NarrationRepository().get_script(script_id)
            if stored is None:
                raise ApplicationError(
                    f"Narration script {script_id} not found", non_retryable=True
                )
        else:
            # No inline script and no script_id: generate from the digest
            generated = await generate_narration_script_activity(payload)
            return generated

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


def _expected_narration_text(spec: NarrationSpec, script: NarrationScript) -> str:
    """Return everything the render is supposed to say (Phase 7.14).

    Mirrors what ``NarrationTTSService`` synthesizes: the optional audible AI
    disclosure is prepended when the spec and the settings ask for it, then
    every segment in order.

    Args:
        spec: The cast/delivery spec used for the render.
        script: The script that was rendered.

    Returns:
        Plain text of the expected narration.
    """
    parts: list[str] = []
    if spec.ai_disclosure and settings.narration_ai_disclosure_text:
        parts.append(settings.narration_ai_disclosure_text)
    parts.extend(segment.text for segment in script.segments)
    return " ".join(parts)


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
        f"Rendering narration collection={collection} run_id={run_id} "
        f"segments={len(script.segments)} strategy={strategy}"
    )
    jobs = AudioJobsRepository()
    await jobs.start_job(run_id, collection, kind=NARRATION_JOB_KIND)
    await jobs.set_generating(run_id, kind=NARRATION_JOB_KIND)
    started = time.monotonic()
    service = NarrationTTSService()
    uses_modal = spec.tts_backend is not TTSBackend.OMNIVOICE
    if uses_modal:
        await _enforce_modal_budget(
            service, spec, script, collection=collection, run_id=run_id, strategy=strategy
        )
    # Measure the real Modal cost of this episode: tag the app so the billing
    # report attributes the interval, snapshot the billing summary before and
    # after, and let each call record its own wall-clock cost.
    set_cost_context(collection=collection, run_id=run_id, stage="tts")
    before = await billing_snapshot() if uses_modal else None
    try:
        async with episode_tags(collection, run_id):
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
    if before is not None:
        await record_billing_delta(
            billing_delta(before, await billing_snapshot()),
            collection=collection,
            run_id=run_id,
            backend=str(spec.tts_backend),
            wall_seconds=elapsed,
        )
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
            f"narration_run_without_digest_row run_id={run_id} audio_path={result.audio_path}"
        )
    # Phase 7.14: verify the audio this render just produced (audio-only gate:
    # the digest workflow withholds the narration delivery when this fails, and
    # it never touches the digest itself). Fail-closed: an unusable file or an
    # empty script scores as an error and withholds the audio too.
    verification: dict[str, Any] | None = None
    audio_verify = payload.get("audio_verify")
    if isinstance(audio_verify, dict) and audio_verify.get("enabled"):
        verify_threshold = audio_verify.get("threshold")
        expected = _expected_narration_text(spec, script)
        verify_started = time.monotonic()

        def _progress(done: int, total: int) -> None:
            activity.heartbeat(done, total)

        try:
            score = await verify_audio(
                result.audio_path,
                expected,
                threshold=float(verify_threshold) if verify_threshold is not None else None,
                on_progress=_progress,
            )
            verification = {
                "passed": score.passed,
                "verdict": score.verdict,
                "score": score.wer,
                "threshold": score.threshold,
                "similarity": score.similarity,
                "model": score.model,
                "language": score.language,
                "block_count": score.block_count,
                "audio_path": result.audio_path,
                "reason": None,
            }
            details: dict[str, Any] = {**score.to_dict(), "audio_path": result.audio_path}
        except AudioVerifyError as exc:
            fallback = (
                settings.audio_verify_wer_threshold
                if verify_threshold is None
                else float(verify_threshold)
            )
            verification = {
                "passed": False,
                "verdict": "error",
                "score": None,
                "threshold": fallback,
                "audio_path": result.audio_path,
                "reason": str(exc),
            }
            details = {"reason": str(exc), "expected_chars": len(expected)}
        await VerificationsRepository().record(
            run_id=run_id,
            collection=collection,
            target="narration_audio",
            method="asr_wer",
            verdict=verification["verdict"],
            score=verification["score"],
            threshold=float(verification["threshold"]),
            passed=bool(verification["passed"]),
            details=details,
            model=str(verification.get("model") or ""),
            duration_seconds=round(time.monotonic() - verify_started, 2),
        )
        activity.logger.info(
            f"Narration audio verified: passed={verification['passed']} "
            f"WER={verification['score']} threshold={verification['threshold']}"
        )

    payload = result.model_dump()
    payload["run_attached"] = bool(updated)
    payload["verification"] = verification
    return payload


@activity.defn
async def generate_narration_script_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate a narration script from a digest using the local LLM (LiteLLM).

    This activity runs inside the narration worker and is called by the
    NarrationWorkflow when the caller does not provide an inline script or a
    stored script_id. It reads the digest from the run, builds the prompt with
    the spec, calls the LLM, validates the returned JSON against the
    NarrationScript model, and returns the script for the render activity.

    Args:
        payload: Dict with ``spec``, ``collection``, optional ``run_id`` and
            ``prompt_template`` (defaults to "default").

    Returns:
        Dict with the validated ``script`` (NarrationScript dump) plus metadata.

    Raises:
        ApplicationError: If the LLM call fails, the response is not valid JSON,
            or the script fails validation. Non-retryable for validation errors.
    """
    import json
    import re

    from pydantic import ValidationError

    from src.api.db.database import RunsRepository
    from src.api.models.narration import NarrationScript, NarrationSpec
    from src.core.llm import generate_completion
    from src.core.prompts import (
        NARRATION_SCRIPT_USER_PROMPT_TEMPLATE,
        build_narration_script_system_prompt,
    )

    raw_spec = payload.get("spec")
    if not isinstance(raw_spec, dict):
        raise ApplicationError("Narration activity needs a 'spec' dict", non_retryable=True)
    try:
        spec = NarrationSpec.model_validate(raw_spec)
    except ValidationError as exc:
        raise ApplicationError(f"Invalid narration spec: {exc}", non_retryable=True) from exc

    run_id = _run_id(payload)
    collection = str(payload.get("collection") or "default")
    prompt_template = str(payload.get("prompt_template") or "default")
    main_story_count = int(payload.get("main_story_count") or 5)
    headline_count = int(payload.get("headline_count") or 5)

    # Fetch the digest text from the run
    row = await RunsRepository().get_run_by_workflow_id(run_id)
    if not row:
        raise ApplicationError(f"No digest run found for workflow_id={run_id}", non_retryable=True)
    digest_markdown = row.get("digest_preview") or row.get("digest") or ""
    if not digest_markdown:
        raise ApplicationError(f"Digest run {run_id} has no digest text", non_retryable=True)

    # The narration script system prompt now has {main_story_count} and {headline_count}
    # placeholders that must be filled with the actual counts.
    system_prompt_template = build_narration_script_system_prompt(prompt_template)
    system_prompt = system_prompt_template.format(
        main_story_count=main_story_count,
        headline_count=headline_count,
    )
    user_prompt = NARRATION_SCRIPT_USER_PROMPT_TEMPLATE.format(
        digest_markdown=digest_markdown,
        spec_json=spec.model_dump_json(indent=2),
    )

    activity.logger.info(
        f"Generating narration script via LLM collection={collection} "
        f"run_id={run_id} template={prompt_template}"
    )

    # A small model asked for a whole podcast script in one shot is unreliable:
    # it may wrap the JSON in prose, truncate it mid-structure, or return
    # nothing. Those are transport/formatting failures, so retrying with a
    # fresh sample can fix them; an unknown role or a bad voice cannot, and is
    # still raised as non-retryable below.
    last_error: str = ""
    script_data: dict[str, Any] | None = None
    for attempt in range(1, _SCRIPT_FORMAT_ATTEMPTS + 1):
        try:
            content = await generate_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4 if attempt == 1 else 0.5,
                max_tokens=_SCRIPT_MAX_TOKENS,
            )
        except Exception as exc:
            last_error = f"LLM call failed: {exc}"
            activity.logger.warning(
                f"narration_script_llm_retry attempt={attempt} reason={last_error}"
            )
            continue

        activity.logger.info(
            f"LLM narration script response length: {len(content)} attempt={attempt}"
        )
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if not json_match:
            last_error = f"no JSON object in response: {content[:200]}"
            activity.logger.warning(f"narration_script_llm_retry attempt={attempt} reason=no_json")
            continue
        try:
            script_data = json.loads(json_match.group(0))
        except json.JSONDecodeError as exc:
            # Usually a truncated response: the script is long and the model
            # ran out of tokens mid-structure.
            last_error = f"invalid JSON: {exc}"
            activity.logger.warning(f"narration_script_llm_retry attempt={attempt} reason=bad_json")
            continue
        break

    if script_data is None:
        raise ApplicationError(
            f"LLM did not return a valid narration script after "
            f"{_SCRIPT_FORMAT_ATTEMPTS} attempts: {last_error}"
        )

    # Validate the script against the spec
    try:
        script = NarrationScript.model_validate(script_data, context={"spec": spec})
    except ValidationError as exc:
        raise ApplicationError(
            f"Generated narration script failed validation: {exc}", non_retryable=True
        ) from exc

    return {
        "script": script.model_dump(),
        "source": "llm",
        "title": script.title,
        "segments": len(script.segments),
        "char_count": script.total_chars(),
    }
