"""One-shot script to manually render a narration (Phase 7).

Starts a single ``NarrationWorkflow`` on the narration task queue, which is the
same path the digest pipeline dispatches to. Useful for debugging the renderer
without waiting for a scheduled digest, and for checking a cast/script before
enabling narration on a collection.

The script can be passed inline (--script) or referenced by the id of a row
already stored by the admin API (--script-id).

Examples:
    python -m src.workers.trigger_narration --collection tech \
        --spec narration-spec.json --script narration-script.json
    python -m src.workers.trigger_narration --collection tech \
        --spec narration-spec.json --script-id 12
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.config import settings
from src.workers.narration_workflows import NarrationConfig, NarrationWorkflow
from src.workers.run_worker import build_client


def _load_json(path: str) -> dict[str, Any]:
    """Read a JSON object from a file.

    Args:
        path: Path to the JSON file.

    Returns:
        The parsed dict.

    Raises:
        SystemExit: If the file is missing or is not a JSON object.
    """
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{source} must contain a JSON object.")
    return payload


async def main() -> None:
    """Start and wait for a manual narration workflow run."""
    parser = argparse.ArgumentParser(description="Render one narration manually")
    parser.add_argument("--collection", default="default", help="Collection name.")
    parser.add_argument(
        "--spec", required=True, help="Path to the NarrationSpec JSON file (cast, music)."
    )
    parser.add_argument("--script", default=None, help="Path to the NarrationScript JSON file.")
    parser.add_argument(
        "--script-id",
        type=int,
        default=None,
        help="Id of a script already stored in narration_scripts.",
    )
    parser.add_argument(
        "--strategy",
        default="turns",
        choices=["turns", "conversations"],
        help="Render strategy. Defaults to one request per turn.",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Existing digest workflow id to attach the narration to.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when NARRATION_ENABLED is false (bypasses the kill switch).",
    )
    args = parser.parse_args()

    if not settings.narration_enabled and not args.force:
        raise SystemExit(
            "Narration is disabled (NARRATION_ENABLED=false). Use --force to render anyway."
        )
    if not args.script and args.script_id is None:
        raise SystemExit("Pass either --script or --script-id.")

    config = NarrationConfig(
        collection=args.collection,
        spec=_load_json(args.spec),
        script=_load_json(args.script) if args.script else None,
        script_id=args.script_id,
        run_id=args.run_id,
        strategy=args.strategy,
    )
    workflow_id = f"narration-{args.collection}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    client = await build_client()
    result = await client.execute_workflow(
        NarrationWorkflow.run,
        config,
        id=workflow_id,
        task_queue=settings.narration_task_queue,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
