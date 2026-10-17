"""Ejemplo: verificación de TTS con voces stock y voice design expresivo.

Uso:
    python docs/examples/tts_voice_design.py [--base-url http://192.168.5.4:8000]

Pasos:
    1. Comprueba /api/v1/health.
    2. Lista las voces stock (GET /api/v1/voices/stock).
    3. Sintetiza una frase con voz stock (POST /api/v1/tts) → stock.wav.
    4. Genera variantes expresivas con tokens de instruct válidos
       (POST /api/v1/tts/instruct) → voice_design_*.wav.

Nota: OmniVoice no soporta emociones formales (happy, sad, ...). Este ejemplo
usa los tokens de voice design soportados por el motor (whisper, pitch, edad)
como alternativa para variar el estilo vocal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

OUTPUT_DIR = Path(__file__).resolve().parent / "out"

# Variantes expresivas usando SOLO tokens válidos de VALID_INSTRUCT_TOKENS_EN.
EXPRESSIVE_VARIANTS: list[dict[str, str]] = [
    {
        "name": "neutral",
        "instruct": "female, young adult, moderate pitch",
        "text": "Hoy es un día cualquiera, nada especial que contar.",
    },
    {
        "name": "whisper_secreto",
        "instruct": "female, whisper",
        "text": "Te voy a contar un secreto, pero que no se entere nadie.",
    },
    {
        "name": "grave_serio",
        "instruct": "male, middle-aged, very low pitch",
        "text": "Atención, esto es un comunicado de máxima importancia.",
    },
    {
        "name": "agudo_energico",
        "instruct": "female, teenager, very high pitch",
        "text": "¡No me lo puedo creer, es la mejor noticia del mundo!",
    },
]


async def check_health(client: httpx.AsyncClient) -> bool:
    """Comprueba que el servidor responde en /api/v1/health."""
    try:
        resp = await client.get("/api/v1/health")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[ERROR] Health check falló: {exc}")
        return False
    print(f"[OK] Health: {resp.json()}")
    return True


async def list_stock_voices(client: httpx.AsyncClient, language: str = "es") -> list[dict]:
    """Lista las voces stock disponibles para un idioma."""
    resp = await client.get("/api/v1/voices/stock", params={"language": language})
    resp.raise_for_status()
    voices = resp.json()
    print(f"[OK] Voces stock ({language}): {[v['voice_id'] for v in voices]}")
    return voices


async def synthesize_stock(
    client: httpx.AsyncClient,
    *,
    text: str,
    voice_id: str,
    language: str,
    output_path: Path,
) -> bool:
    """Sintetiza con voz stock y guarda el WAV. Devuelve True si OK."""
    payload = {"text": text, "voice_id": voice_id, "language": language}
    try:
        resp = await client.post("/api/v1/tts", json=payload, timeout=120.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"[ERROR] TTS stock {voice_id}: HTTP {exc.response.status_code} → {exc.response.text}"
        )
        return False
    except httpx.HTTPError as exc:
        print(f"[ERROR] TTS stock {voice_id}: {exc}")
        return False

    output_path.write_bytes(resp.content)
    print(f"[OK] Stock '{voice_id}' → {output_path} ({len(resp.content)} bytes)")
    return True


async def synthesize_instruct(
    client: httpx.AsyncClient,
    *,
    text: str,
    instruct: str,
    language: str,
    output_path: Path,
) -> bool:
    """Sintetiza con instruct de voice design y guarda el WAV."""
    payload = {"text": text, "instruct": instruct, "language": language}
    try:
        resp = await client.post("/api/v1/tts/instruct", json=payload, timeout=120.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"[ERROR] Instruct '{instruct}': HTTP {exc.response.status_code} → {exc.response.text}"
        )
        return False
    except httpx.HTTPError as exc:
        print(f"[ERROR] Instruct '{instruct}': {exc}")
        return False

    output_path.write_bytes(resp.content)
    print(f"[OK] Instruct '{instruct}' → {output_path} ({len(resp.content)} bytes)")
    return True


async def main(base_url: str) -> int:
    """Ejecuta la verificación y los ejemplos expresivos."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0

    async with httpx.AsyncClient(base_url=base_url) as client:
        if not await check_health(client):
            return 1

        voices = await list_stock_voices(client, language="es")
        if not voices:
            print("[ERROR] No hay voces stock en español disponibles.")
            return 1

        # 1) Verificación del flujo principal: TTS con voz stock.
        voice_id = voices[0]["voice_id"]
        ok = await synthesize_stock(
            client,
            text="Hola, esto es una prueba de síntesis con voz stock.",
            voice_id=voice_id,
            language="es",
            output_path=OUTPUT_DIR / "stock.wav",
        )
        if not ok:
            failures += 1

        # 2) Variantes expresivas con voice design (tokens válidos de instruct).
        for variant in EXPRESSIVE_VARIANTS:
            ok = await synthesize_instruct(
                client,
                text=variant["text"],
                instruct=variant["instruct"],
                language="es",
                output_path=OUTPUT_DIR / f"voice_design_{variant['name']}.wav",
            )
            if not ok:
                failures += 1

    if failures:
        print(f"\n[RESULTADO] {failures} petición(es) fallaron. Revisa los mensajes anteriores.")
        return 1
    print(f"\n[RESULTADO] Todo OK. Audios generados en: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verificación TTS stock + voice design expresivo.")
    parser.add_argument(
        "--base-url",
        default="http://192.168.5.4:8000",
        help="URL base del servidor (por defecto http://192.168.5.4:8000)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.base_url)))
