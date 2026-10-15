"""Run the VoxCPM2 gate benchmark (Phase 10.10).

VoxCPM2 is implemented but inactive. This script measures the criteria that
decide whether it replaces the current renderer, using a real reference voice:

1. Native sample rate is 48 kHz (genuine high fidelity, not upsampled)
2. No clipping in the best take
3. DNSMOS no-reference P.835 overall MOS >= 3.0
4. Real wideband content above 16 kHz

It also reports the best-of-N MOS gain. It runs on Modal, so it needs
MODAL_ENABLED=true and credentials.

Examples:
    python scripts/benchmark_voxcpm.py --voice anchor_neutral_m
    python scripts/benchmark_voxcpm.py --all
    python scripts/benchmark_voxcpm.py --voice anchor_neutral_m --text "Hello."
    python scripts/benchmark_voxcpm.py --voice anchor_neutral_m --candidates 5
"""

import argparse
import asyncio
import json
import sys

from src.core.config import settings
from src.core.voice_catalog import load_manifest

DEFAULT_TEXT = "Welcome to the daily briefing. Here is what you need to know this morning."


async def _run(voice_ref: str, text: str, language: str, num_candidates: int) -> dict:
    """Call the Modal benchmark function and return the report.

    Args:
        voice_ref: Reference voice name.
        text: Text to synthesize.
        language: ISO 639-1 language code.
        num_candidates: Takes to synthesize before DNSMOS picks the best.

    Returns:
        The benchmark report.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    function = modal.Function.from_name(settings.modal_app_name, "benchmark_voxcpm2")
    return await function.remote.aio(
        voice_ref=voice_ref, text=text, language=language, num_candidates=num_candidates
    )


async def main() -> None:
    """Run the benchmark for one or every catalog voice."""
    parser = argparse.ArgumentParser(description="VoxCPM2 gate benchmark")
    parser.add_argument("--voice", default=None, help="Reference voice name")
    parser.add_argument("--all", action="store_true", help="Every generated voice")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="Text to synthesize")
    parser.add_argument(
        "--candidates",
        type=int,
        default=settings.voxcpm2_candidates,
        help="Takes synthesized before DNSMOS picks the best",
    )
    args = parser.parse_args()

    manifest = load_manifest()
    if args.voice:
        names = [args.voice]
    elif args.all:
        names = sorted(manifest)
    else:
        parser.error("choose --voice NAME or --all")
    if not names:
        raise SystemExit("no generated voices yet: run design_voice first")

    failed = False
    for name in names:
        entry = manifest.get(name)
        if entry is None:
            print(f"SKIP {name}: not in the manifest")
            continue
        language = "en" if not name.startswith("es_") else "es"
        report = await _run(name, args.text, language, args.candidates)
        print(json.dumps(report, indent=2))
        if not report["pass"]:
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
