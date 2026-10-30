#!/usr/bin/env python3
"""Quick Kokoro vs other engines benchmark (uses gateway /tts endpoint if available).

Usage:
  python scripts/benchmark_kokoro.py --gateway http://localhost:8001 --text "Hola mundo"
"""

import argparse
import asyncio
import json
import time

import httpx


async def bench(gateway: str, text: str, voices: list[str], lang: str = "es"):
    # The API mounts every router under /api/v1; accept both spellings so
    # `--gateway http://localhost:8001` (the documented usage) works.
    base = gateway.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    async with httpx.AsyncClient(timeout=60.0) as client:
        for v in voices:
            for engine in [None, "kokoro", "pocket_tts"]:
                payload = {"text": text, "voice_id": v, "language": lang, "speed": 1.0}
                if engine:
                    payload["engine"] = engine
                t0 = time.perf_counter()
                try:
                    r = await client.post(f"{base}/tts", json=payload)
                    dt = time.perf_counter() - t0
                    print(
                        json.dumps(
                            {
                                "voice": v,
                                "engine": engine or "auto",
                                "status": r.status_code,
                                "time_s": round(dt, 3),
                                "bytes": len(r.content),
                            }
                        )
                    )
                except Exception as e:
                    dt = time.perf_counter() - t0
                    print(
                        json.dumps(
                            {
                                "voice": v,
                                "engine": engine or "auto",
                                "error": str(e),
                                "time_s": round(dt, 3),
                            }
                        )
                    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", default="http://localhost:8001")
    p.add_argument("--text", default="Esto es una prueba de Kokoro en español.")
    p.add_argument("--voices", default="es-mx-male")
    p.add_argument("--lang", default="es")
    args = p.parse_args()
    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    asyncio.run(bench(args.gateway, args.text, voices, args.lang))


if __name__ == "__main__":
    main()
