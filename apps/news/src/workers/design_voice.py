"""Generate reference voices for the catalog (Phase 7.8/7.9).

Starts a ``VoiceCatalogWorkflow`` in the voices namespace, which designs every
missing or stale profile with Qwen3-TTS VoiceDesign on the GPU box, stores the
reference locally and records it in the manifest.

Examples:
    python -m src.workers.design_voice --all
    python -m src.workers.design_voice --name anchor_neutral_m
    python -m src.workers.design_voice --only-missing
    python -m src.workers.design_voice --all --upload
    python -m src.workers.design_voice --dry-run
"""

import argparse
import asyncio
import json

from src.core.config import settings
from src.core.voice_catalog import load_manifest, load_profiles, missing_or_stale
from src.workers.run_worker import build_client
from src.workers.voice_workflows import VoiceCatalogConfig, VoiceCatalogWorkflow


def _pending(names: list[str], only_missing: bool) -> list[str]:
    """Return the profile names the run would (re)generate.

    Args:
        names: Optional subset of profile names.
        only_missing: When True, skip profiles that are merely stale.

    Returns:
        Sorted profile names.
    """
    profiles = load_profiles()
    by_name = {profile.name: profile for profile in profiles}
    targets = [by_name[name] for name in names if name in names] if names else profiles
    pending = set(missing_or_stale(targets, load_manifest()))
    if only_missing:
        pending = {name for name in pending if name not in load_manifest()}
    return sorted(pending)


async def main() -> None:
    """Start and wait for a voice catalog run."""
    parser = argparse.ArgumentParser(description="Generate reference voices")
    parser.add_argument("--all", action="store_true", help="Every enabled profile")
    parser.add_argument("--name", action="append", default=[], help="Profile name (repeatable)")
    parser.add_argument(
        "--only-missing", action="store_true", help="Skip profiles that are merely stale"
    )
    parser.add_argument("--upload", action="store_true", help="Also upload to the Modal Volume")
    parser.add_argument(
        "--modal",
        action="store_true",
        help="Design on Modal instead of the local GPU worker (Phase 10.4; needs MODAL_ENABLED)",
    )
    parser.add_argument("--dry-run", action="store_true", help="List pending profiles and exit")
    args = parser.parse_args()

    names = args.name or (["*"] if args.all else [])
    if names == ["*"]:
        names = []
    pending = _pending(names, args.only_missing)

    if args.dry_run:
        print(json.dumps({"pending": pending}, indent=2))
        return
    if not pending:
        print(json.dumps({"pending": [], "results": []}, indent=2))
        return

    config = VoiceCatalogConfig(
        names=names,
        upload=args.upload,
        run_in_modal=args.modal,
        task_queue=settings.voice_catalog_task_queue,
        design_task_queue=settings.voice_design_task_queue,
    )
    client = await build_client(settings.temporal_voices_namespace)
    result = await client.execute_workflow(
        VoiceCatalogWorkflow.run,
        config,
        id=f"voice-catalog-{asyncio.get_running_loop().time():.0f}",
        task_queue=settings.voice_catalog_task_queue,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
