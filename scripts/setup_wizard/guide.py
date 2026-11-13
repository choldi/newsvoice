"""Paginated guide for the external credentials asked by the setup wizard.

The guide is shown before the welcome menu so an operator can decide which
providers to enable and collect the required tokens before filling the forms.
"""

from __future__ import annotations

import curses
from dataclasses import dataclass

from .curses_ui import App, Row, Screen

__all__ = ["GUIDE_PAGES", "GuidePage", "external_guide_screen"]


@dataclass(frozen=True)
class GuidePage:
    """One page of the external values guide.

    Attributes:
        title: Page heading shown in the panel subtitle.
        lines: Paragraphs and action items, rendered as wrapped notes.
    """

    title: str
    lines: tuple[str, ...]


GUIDE_PAGES: tuple[GuidePage, ...] = (
    GuidePage(
        "Antes de empezar · qué es externo",
        (
            "Solo los valores marcados [externo] dependen de servicios de terceros.",
            "Los [interno] los aporta el stack: puertos, TTS local, voces y claves compartidas.",
            "Puedes dejar vacío un proveedor que no vayas a usar: su engine queda desactivado.",
            "Rellena solo lo que necesites ahora; el resto se puede añadir después con "
            "«Editar los .env ya configurados».",
            "",
            "Enlaces de referencia:",
            "· Fish Audio: https://fish.audio/",
            "· Guía de API de Fish: https://docs.fish.audio/developer-guide/getting-started/api-key",
            "· HuggingFace Pocket TTS: https://huggingface.co/kyutai/pocket-tts",
            "· Telegram BotFather: https://t.me/BotFather",
            "· Contraseñas de aplicación de Gmail: https://myaccount.google.com/apppasswords",
            "",
            "Acción: elige perfil y motor TTS; después rellena solo los proveedores de ese motor.",
        ),
    ),
    GuidePage(
        "Fish Audio · TTS cloud",
        (
            "Cuándo: solo si usas fish_tts o lo añades a TTS_EXTRA_ENGINES.",
            "Qué necesitas: FISH_API_KEY, FISH_MODEL (por defecto s2.1-pro-free) y "
            "FISH_API_BASE_URL (normalmente https://api.fish.audio).",
            "",
            "Cómo obtener la clave:",
            "1. Crea una cuenta en https://fish.audio/ y verifica el email.",
            "2. Entra en «Get Started» y pulsa «Get your API key».",
            "3. Copia la clave en el momento: solo se muestra una vez.",
            "4. Pégala en «Fish API key»; no la subas al repositorio.",
            "",
            "Coste y errores: el modelo s2.1-pro-free funciona sin crédito. Los modelos de "
            "pago devuelven 402 si no hay saldo; 401 indica clave incorrecta y 429 un límite "
            "de peticiones.",
            "Documentación: https://docs.fish.audio/developer-guide/getting-started/api-key",
            "",
            "Alternativa sin cuenta: deja Fish vacío y usa pocket_tts o edgetts en local.",
        ),
    ),
    GuidePage(
        "HuggingFace · clonado con pocket_tts",
        (
            "Cuándo: solo para clonar voces con pocket_tts. Las voces stock no necesitan token.",
            "Qué necesitas: HF_TOKEN con permiso de lectura. HF_TOKEN_FILE es la alternativa "
            "avanzada para montar el token como archivo.",
            "",
            "Qué aprobar:",
            "1. Entra en https://huggingface.co/kyutai/pocket-tts con tu cuenta.",
            "2. Pulsa «Agree and access repository» para aceptar los términos del modelo gated.",
            "3. Crea un token de lectura en https://huggingface.co/settings/tokens.",
            "4. Pega el token en «Token HF» o ejecuta «hf auth login» en el host.",
            "",
            "Sin token, el clonado falla; las voces stock siguen funcionando. Si cambias de "
            "cuenta, vuelve a aceptar los términos con la cuenta nueva.",
            "Seguridad: no compartas el token ni lo incluyas en git.",
        ),
    ),
    GuidePage(
        "Telegram · entrega de boletines",
        (
            "Cuándo: solo si quieres entregar el boletín por Telegram.",
            "Qué necesitas: TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID.",
            "",
            "Acciones:",
            "1. Abre https://t.me/BotFather y envía /newbot.",
            "2. Elige nombre y usuario del bot; copia el token que te devuelve.",
            "3. Envía un mensaje al bot y abre "
            "https://api.telegram.org/bot<TOKEN>/getUpdates para ver chat.id.",
            "4. Pega el token y el chat id en la sección de entrega.",
            "",
            "Si el bot está en un grupo, añádelo al grupo y usa el id negativo del grupo. "
            "Para tu id personal también puedes usar @userinfobot.",
            "Opcional: deja ambos valores vacíos para desactivar Telegram.",
        ),
    ),
    GuidePage(
        "SMTP · correo",
        (
            "Cuándo: solo para entregar por email o enviar códigos de acceso.",
            "Qué necesitas: SMTP_HOST, SMTP_PORT, SMTP_USER y SMTP_PASSWORD.",
            "",
            "Gmail:",
            "1. Activa la verificación en dos pasos en tu cuenta.",
            "2. Crea una contraseña de aplicación en https://myaccount.google.com/apppasswords.",
            "3. Usa smtp.gmail.com, puerto 587 y esa contraseña de aplicación, no la de tu cuenta.",
            "",
            "Acción: rellena usuario y contraseña de aplicación. Si no usas correo, deja "
            "SMTP_USER vacío para desactivarlo.",
            "Aviso: Gmail rechaza el acceso con la contraseña normal de la cuenta.",
        ),
    ),
    GuidePage(
        "LLM · generación de boletines",
        (
            "Cuándo: siempre que el perfil genere boletines; es el modelo que escribe.",
            "Qué necesitas: LLM_API_BASE (incluye /v1) y LLM_MODEL.",
            "",
            "Ejemplo local con llama.cpp:",
            "· En Docker: http://host.docker.internal:8080/v1",
            "· En Linux nativo: http://127.0.0.1:8080/v1",
            "· LLM_MODEL debe ser el nombre exacto del modelo cargado.",
            "",
            "Acción: arranca tu servidor OpenAI-compatible y pega la URL y el modelo. "
            "Comprueba con «curl <LLM_API_BASE>/models» que el modelo aparece en la lista.",
            "Si tu endpoint pide autenticación, ponlo detrás de un proxy: el asistente no "
            "guarda una clave LLM.",
            "En Docker, host.docker.internal apunta al host; en Linux puede requerir extra_hosts.",
        ),
    ),
    GuidePage(
        "OpenAI-compatible TTS · opcional",
        (
            "Cuándo: solo si usas openai_tts o lo añades a TTS_EXTRA_ENGINES.",
            "Qué necesitas: OPENAI_TTS_BASE_URL con /v1, OPENAI_TTS_API_KEY si el proveedor "
            "la exige y OPENAI_TTS_MODEL (por defecto tts-1).",
            "",
            "OpenAI:",
            "· Crea una clave en https://platform.openai.com/api-keys.",
            "· Usa https://api.openai.com/v1 como base y un modelo disponible en tu cuenta.",
            "· Cualquier endpoint compatible con POST /audio/speech también sirve.",
            "",
            "Acción: rellena base, clave y modelo; deja la base vacía para desactivarlo. "
            "Si usas Fish, no necesitas configurar OpenAI TTS.",
        ),
    ),
    GuidePage(
        "Resumen · acciones antes de continuar",
        (
            "1. Elige perfil y método de instalación.",
            "2. Elige motor TTS: pocket_tts y edgetts no necesitan cuentas; Fish y OpenAI sí.",
            "3. Rellena solo los [externo] de los servicios que vas a usar; deja vacío el resto.",
            "4. Genera claves internas con «g» y recupera valores por defecto con Ctrl+R.",
            "5. Pulsa «a» para ver opciones avanzadas y F1 para explicar cada campo.",
            "6. Guarda y valida; cada error indica el valor o comando que falta.",
            "",
            "Enlaces:",
            "· Fish: https://docs.fish.audio/developer-guide/getting-started/api-key",
            "· HuggingFace: https://huggingface.co/kyutai/pocket-tts",
            "· Telegram: https://t.me/BotFather",
            "· Gmail: https://myaccount.google.com/apppasswords",
            "",
            "Acción: pulsa «Continuar al menú» o Esc para volver al menú.",
        ),
    ),
)


def external_guide_screen(app: App) -> Screen:
    """Build the paginated external-values guide.

    Args:
        app: Running application, used to refresh the current page.

    Returns:
        A screen that changes its rows when the page changes.
    """
    state = {"page": 0}

    def render() -> None:
        page = GUIDE_PAGES[state["page"]]
        screen = app.current
        screen.subtitle = page.title
        screen.progress = f"{state['page'] + 1}/{len(GUIDE_PAGES)}"
        rows = [Row("note", label=line) for line in page.lines]
        rows.append(Row("note", label=""))
        if state["page"] > 0:
            rows.append(Row("button", label="Página anterior", action="prev"))
        if state["page"] < len(GUIDE_PAGES) - 1:
            rows.append(Row("button", label="Página siguiente", action="next"))
        else:
            rows.append(Row("button", label="Continuar al menú", action="done"))
        rows.append(Row("button", label="Saltar al menú", action="done"))
        screen.rows = rows
        app.refocus()

    def move(delta: int) -> None:
        state["page"] = min(max(state["page"] + delta, 0), len(GUIDE_PAGES) - 1)
        render()

    def on_key(app_: App, key: int) -> bool:
        if key in (curses.KEY_LEFT, curses.KEY_PPAGE):
            move(-1)
            return True
        if key in (curses.KEY_RIGHT, curses.KEY_NPAGE):
            move(1)
            return True
        if key == curses.KEY_HOME:
            state["page"] = 0
            render()
            return True
        if key == curses.KEY_END:
            state["page"] = len(GUIDE_PAGES) - 1
            render()
            return True
        return False

    def on_enter(app_: App) -> None:
        render()

    def activate(app_: App, row: Row) -> None:
        if row.action == "prev":
            move(-1)
        elif row.action == "next":
            move(1)
        elif row.action == "done":
            app_.pop()

    return Screen(
        title="Guía de valores externos",
        subtitle=GUIDE_PAGES[0].title,
        rows=[],
        footer="←/→ o PgUp/PgDn Página   ↑/↓ Mover   Enter Botón   Esc Menú",
        progress=f"1/{len(GUIDE_PAGES)}",
        on_enter=on_enter,
        on_activate=activate,
        on_key=on_key,
    )
