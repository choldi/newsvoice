"""Example: Voice cloning workflow using the TTS API.

This script demonstrates the complete voice cloning lifecycle:
1. Clone a voice from a reference audio file
2. List cloned voices
3. Synthesize speech with the cloned voice
4. Clean up (delete the cloned voice)

Prerequisites:
    - TTS API server running (make run)
    - A reference audio file (WAV, 0.5-30 seconds, 8000-48000 Hz)

Usage:
    python docs/examples/clone_voice.py /path/to/reference.wav
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

API_BASE = "http://localhost:8000/api/v1"


async def clone_voice(
    client: httpx.AsyncClient,
    name: str,
    language: str,
    audio_path: Path,
) -> str:
    """Clone a voice from a reference audio file."""
    print(f"\n--- Cloning voice '{name}' ---")
    with open(audio_path, "rb") as f:
        response = await client.post(
            f"{API_BASE}/voices/clone",
            data={"name": name, "language": language},
            files={"reference_audio": (audio_path.name, f, "audio/wav")},
        )
    response.raise_for_status()
    result = response.json()
    voice_id = result["voice_id"]
    print(f"Cloned voice ID: {voice_id}")
    return voice_id


async def list_cloned_voices(client: httpx.AsyncClient) -> list[dict]:
    """List all cloned voices."""
    print("\n--- Listing cloned voices ---")
    response = await client.get(f"{API_BASE}/voices/cloned")
    response.raise_for_status()
    voices = response.json()
    for v in voices:
        print(f"  {v['id'][:8]}... | {v['name']} | {v['language']} | {v['duration_sec']:.1f}s")
    return voices


async def synthesize_with_cloned(
    client: httpx.AsyncClient,
    voice_id: str,
    text: str,
    output_path: Path,
) -> None:
    """Synthesize speech using a cloned voice."""
    print("\n--- Synthesizing with cloned voice ---")
    print(f"Text: {text!r}")
    response = await client.post(
        f"{API_BASE}/tts",
        json={
            "text": text,
            "voice_id": voice_id,
            "language": "es",
            "speed": 1.0,
        },
    )
    response.raise_for_status()
    output_path.write_bytes(response.content)
    print(f"Saved to: {output_path} ({len(response.content)} bytes)")


async def delete_voice(client: httpx.AsyncClient, voice_id: str) -> None:
    """Delete a cloned voice."""
    print(f"\n--- Deleting voice {voice_id[:8]}... ---")
    response = await client.delete(f"{API_BASE}/voices/cloned/{voice_id}")
    response.raise_for_status()
    print("Deleted successfully")


async def main(audio_path: Path) -> None:
    """Run the full voice cloning workflow."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Check server health
        health = await client.get(f"{API_BASE}/health")
        if health.status_code != 200:
            print(f"Error: Server not responding (status={health.status_code})")
            sys.exit(1)
        print("Server is healthy")

        # Clone voice
        voice_id = await clone_voice(
            client,
            name="mi-voz-clonada",
            language="es",
            audio_path=audio_path,
        )

        # List voices
        await list_cloned_voices(client)

        # Synthesize
        output = Path("output_cloned.wav")
        await synthesize_with_cloned(
            client,
            voice_id=voice_id,
            text="Hola, esta es mi voz clonada hablando con la API de OmniVoice.",
            output_path=output,
        )

        # Cleanup
        await delete_voice(client, voice_id)

        # Verify deletion
        await list_cloned_voices(client)

        print("\nDone!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <reference_audio.wav>")
        sys.exit(1)
    audio = Path(sys.argv[1])
    if not audio.exists():
        print(f"Error: File not found: {audio}")
        sys.exit(1)
    asyncio.run(main(audio))
