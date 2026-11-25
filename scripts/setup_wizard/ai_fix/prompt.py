"""Build the repair prompt from the collected evidence, without secrets."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import Context
    from .evidence import Evidence

__all__ = ["build_prompt"]

_SECRET_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|_HASH$|_KEY$|API_KEY|PRIVATE|CREDENTIAL|CHAT_ID|EMAIL|PHONE)",
    re.IGNORECASE,
)
_PLACEHOLDERS = frozenset({"", "EMPTY", "true", "false", "yes", "no", "0", "1"})

_RULES = """Reglas obligatorias del proyecto:
- Python 3.11+, FastAPI, Pydantic v2, structlog, pydantic-settings y Temporal.
- Las llamadas LLM pasan por apps/news/src/core/llm.py (LiteLLM), nunca por el SDK de OpenAI.
- Los prompts del LLM viven en apps/news/src/core/prompts.py.
- Sin secretos ni valores hardcoded en el código; los secretos van en .env.
- ids TTS canónicos en libs/tts_contract; aliases legacy solo en lectura.
- Estilo ruff, línea máxima de 100 caracteres, type hints y docstrings Google style.
- Cambios mínimos y verificables: no refactorices código sin relación con el problema."""

_CONTRACT = """Responde SOLO con un objeto JSON con esta forma:
{
  "status": "ok",
  "diagnosis": "resumen breve",
  "root_causes": ["causa 1"],
  "fixes": [{"path": "ruta/relativa.py", "content": "contenido completo final",
             "reason": "por qué", "confidence": "high|medium|low"}],
  "env_fixes": [{"file": "news", "key": "CLAVE_NO_SECRETA", "value": "valor", "reason": "por qué"}],
  "manual": ["problemas que no puedes arreglar"]
}
Reglas del JSON: 'content' es el archivo COMPLETO resultante; no propongas secretos;
no incluyas archivos .env, binarios, datos ni rutas fuera de apps/, libs/ o scripts/."""


def _secret_values(ctx: Context) -> list[str]:
    """Collect secret-looking values so they never reach the external LLM."""
    values: set[str] = set()
    for env in ctx.envs.values():
        for entry in env.entries():
            if entry.key and entry.value and _SECRET_KEY.search(entry.key):
                if entry.value not in _PLACEHOLDERS and len(entry.value) >= 3:
                    values.add(entry.value)
    for key, value in os.environ.items():
        if _SECRET_KEY.search(key) and value and value not in _PLACEHOLDERS and len(value) >= 3:
            values.add(value)
    return sorted(values, key=len, reverse=True)


def _redact(text: str, values: list[str]) -> str:
    """Replace known secret values with a fixed marker."""
    for value in values:
        text = text.replace(value, "<redactado>")
    return text


def build_prompt(ctx: Context, evidence: Evidence, *, allow_paths: tuple[str, ...]) -> str:
    """Assemble a redacted prompt for the ephemeral agent."""
    values = _secret_values(ctx)
    parts = [
        "Solventa los problemas de este repositorio devolviendo correcciones aplicables.",
        _RULES,
        "## Validación del setup",
        "\n".join(evidence.checks) or "sin errores ni avisos",
    ]
    if evidence.deploy_report:
        parts.extend(["## Informe de despliegue", evidence.deploy_report])
    if evidence.logs:
        parts.extend(["## Logs recientes", evidence.logs])
    if evidence.tests:
        parts.extend(["## Tests y lint", evidence.tests])
    if evidence.user_text:
        parts.extend(["## Descripción del usuario", evidence.user_text])
    if evidence.files:
        parts.extend(["## Archivos candidatos", "\n".join(evidence.files)])
    parts.extend(
        [
            "## Rutas permitidas para correcciones",
            ", ".join(allow_paths),
            _CONTRACT,
        ]
    )
    return _redact("\n\n".join(parts), values)
