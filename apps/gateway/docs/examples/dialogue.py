"""Example: Multi-voice dialogue using the TTS API.

This script demonstrates the conversations endpoint to generate
a dialogue between two different voices.

Prerequisites:
    - TTS API server running (make run)

Usage:
    python docs/examples/dialogue.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

API_BASE = "http://localhost:8000/api/v1"


async def generate_dialogue(
    client: httpx.AsyncClient,
    output_path: Path,
) -> None:
    """Generate a dialogue between two voices."""
    print("--- Generating multi-voice dialogue ---")

    response = await client.post(
        f"{API_BASE}/conversations",
        json={
            "turns": [
                {"voice_id": "es-mx-male", "text": "Hola, ¿cómo estás hoy?"},
                {"voice_id": "es-mx-female", "text": "Muy bien, gracias. ¿Y tú?"},
                {"voice_id": "es-mx-male", "text": "Excelente, trabajando en el proyecto."},
                {"voice_id": "es-mx-female", "text": "¡Genial! Cuéntame más."},
            ],
            "pause_ms": 400,
        },
    )
    response.raise_for_status()
    output_path.write_bytes(response.content)
    print(f"Saved to: {output_path} ({len(response.content)} bytes)")


async def main() -> None:
    """Run the dialogue example."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Check server health
        health = await client.get(f"{API_BASE}/health")
        if health.status_code != 200:
            print(f"Error: Server not responding (status={health.status_code})")
            sys.exit(1)
        print("Server is healthy")

        output = Path("output_dialogue.wav")
        await generate_dialogue(client, output)

        print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
