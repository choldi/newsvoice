"""Configuration validation shared by the TUI and the CLI fallback."""

from __future__ import annotations

from dataclasses import dataclass

from .detect import Report
from .envfile import EnvFile

__all__ = ["Check", "validate"]

_PLACEHOLDER_MARKERS = ("your_", "example", "cambiar", "changeme", "<", ">")


@dataclass
class Check:
    """Result of one validation rule.

    Attributes:
        name: Short rule name.
        status: ``ok``, ``warn`` or ``bad``.
        detail: What was found and, when relevant, how to fix it.
    """

    name: str
    status: str
    detail: str


def validate(
    envs: dict[str, EnvFile],
    report: Report | None,
    *,
    will_write: bool,
    profile: str = "",
) -> list[Check]:
    """Run every configuration rule.

    Args:
        envs: Loaded env files keyed by ``deploy`` / ``news`` / ``gateway``.
        report: Host detection, used for the prerequisite rules.
        will_write: True while the wizard can still fix things, which turns
            missing files into warnings instead of failures.
        profile: Selected deployment profile (``docker``, ``local``,
            ``split``, ``audit``). It decides how harsh a missing value is:
            a container cannot honour ``localhost`` defaults, the host can.

    Returns:
        Checks in display order.
    """
    checks: list[Check] = []

    # 1. Files present -----------------------------------------------------
    for key in ("deploy", "news", "gateway"):
        env = envs.get(key)
        if env is None:
            continue
        if env.exists:
            checks.append(Check(f"{key}/.env", "ok", f"{env.key_path} presente"))
        elif will_write:
            checks.append(
                Check(f"{key}/.env", "warn", f"{env.key_path} aún no existe (se escribirá)")
            )
        else:
            checks.append(
                Check(f"{key}/.env", "bad", f"falta {env.key_path} (copia de su .env.example)")
            )

    # 2. Required values ----------------------------------------------------
    # The severity follows what the software really does without the key:
    # compose falls back to `${API_PORT:-8000}` (a warning), while the digest
    # cannot produce anything without an LLM endpoint (an error).
    # Inside a container ``http://localhost:8080/v1`` (the code default) is the
    # container itself, so a missing LLM endpoint is fatal for the docker
    # profiles and only a hint for a host run or an audit.
    llm_severity = "warn" if profile in ("local", "audit") else "bad"
    required = {
        "deploy": (("API_PORT", "warn"), ("GATEWAY_PORT", "warn")),
        "news": (("LLM_API_BASE", llm_severity),),
        "gateway": (("TTS_ENGINE", "warn"),),
    }
    fallback = {
        "API_PORT": "compose usa 8000 por defecto",
        "GATEWAY_PORT": "compose usa 8001 por defecto",
    }
    for key, names in required.items():
        env = envs.get(key)
        if env is None:
            continue
        missing = [(name, sev) for name, sev in names if not env.get(name).strip()]
        if not missing:
            checks.append(Check(f"{key} obligatorias", "ok", "todos los valores obligatorios"))
            continue
        worst = "bad" if any(sev == "bad" for _name, sev in missing) else "warn"
        detail = "falta: " + ", ".join(
            name + (f" ({fallback[name]})" if name in fallback else "") for name, _sev in missing
        )
        checks.append(Check(f"{key} obligatorias", worst, detail))

    # 3. Placeholders ------------------------------------------------------
    from .settings import fields_for

    for key, env in envs.items():
        owned = [field.key for field in fields_for(key)]
        stale = [
            name
            for name in owned
            if env.has(name)
            and any(marker in env.get(name).lower() for marker in _PLACEHOLDER_MARKERS)
        ]
        if stale:
            checks.append(
                Check(
                    f"{key} plantillas",
                    "warn",
                    "siguen en modo plantilla: " + ", ".join(sorted(stale)),
                )
            )

    # 4. Shared gateway key ------------------------------------------------
    # compose injects deploy/GATEWAY_API_KEY into the gateway container while
    # news-engine sends TTS_API_KEY as X-API-Key: three copies of one secret.
    # A divergence is a runtime 401, so it is reported as an error together
    # with the fix the wizard offers.
    deploy = envs.get("deploy")
    news = envs.get("news")
    gateway = envs.get("gateway")
    if deploy is not None:
        shared = deploy.get("GATEWAY_API_KEY").strip()
        consumers: list[tuple[str, str]] = []
        if news is not None:
            consumers.append(("news.TTS_API_KEY", news.get("TTS_API_KEY").strip()))
        if gateway is not None:
            consumers.append(("gateway.API_KEY", gateway.get("API_KEY").strip()))
        fix = (
            " — unifica los tres valores (el asistente lo hace con "
            "«Unificar la clave del gateway»)"
        )
        conflict = [name for name, value in consumers if shared and value != shared and value]
        absent = [name for name, value in consumers if not value]
        if shared:
            if conflict:
                checks.append(
                    Check(
                        "clave compartida",
                        "bad",
                        "valores distintos a GATEWAY_API_KEY: " + ", ".join(conflict) + fix,
                    )
                )
            elif absent:
                checks.append(
                    Check(
                        "clave compartida",
                        "warn",
                        "coincide con GATEWAY_API_KEY; falta copiar a: " + ", ".join(absent),
                    )
                )
            else:
                checks.append(Check("clave compartida", "ok", "coincide con GATEWAY_API_KEY"))
        else:
            filled = [name for name, value in consumers if value]
            if len(set(value for _name, value in consumers if value)) > 1:
                checks.append(
                    Check(
                        "clave compartida",
                        "bad",
                        "GATEWAY_API_KEY vacía y los consumidores no coinciden: "
                        + ", ".join(filled)
                        + fix,
                    )
                )
            else:
                checks.append(
                    Check(
                        "clave compartida",
                        "warn",
                        "GATEWAY_API_KEY vacía: el gateway queda abierto en la red",
                    )
                )

    # 5. Admin credential --------------------------------------------------
    if news is not None:
        has_credential = bool(
            news.get("ADMIN_USERNAME").strip()
            or news.get("ADMIN_PASSWORD_HASH").strip()
            or news.get("ADMIN_API_TOKEN").strip()
        )
        checks.append(
            Check(
                "acceso al panel",
                "ok" if has_credential else "warn",
                "credencial admin definida"
                if has_credential
                else "panel sin credencial: queda abierto",
            )
        )
        if not news.get("DOC_TTS_LINK_SECRET").strip():
            checks.append(
                Check(
                    "enlaces firmados",
                    "warn",
                    "DOC_TTS_LINK_SECRET vacío: el subido de documentos responde 503",
                )
            )
        else:
            checks.append(Check("enlaces firmados", "ok", "DOC_TTS_LINK_SECRET definida"))

    # 6. Port consistency --------------------------------------------------
    if deploy is not None and news is not None:
        left = deploy.get("API_PORT").strip()
        right = news.get("API_PORT").strip()
        if left and right and left != right:
            checks.append(
                Check("puertos", "bad", f"deploy.API_PORT={left} pero news.API_PORT={right}")
            )
        elif left:
            checks.append(
                Check(
                    "puertos", "ok", f"API {left} / gateway {deploy.get('GATEWAY_PORT') or '8001'}"
                )
            )

    # 7. Gateway URL vs backend -------------------------------------------
    if news is not None:
        backend = news.get("TTS_DEFAULT_BACKEND") or "edge"
        url = news.get("TTS_API_BASE_URL").strip()
        if backend != "edge" and not url:
            checks.append(
                Check(
                    "gateway url",
                    "bad",
                    f"TTS_DEFAULT_BACKEND={backend} necesita TTS_API_BASE_URL",
                )
            )
        elif not url:
            checks.append(
                Check(
                    "gateway url",
                    "warn",
                    "sin URL de gateway: el backend por defecto «edge» no la necesita",
                )
            )
        elif url.startswith("http://localhost:8000") or url.startswith("http://127.0.0.1:8000"):
            checks.append(
                Check(
                    "gateway url",
                    "warn",
                    "TTS_API_BASE_URL apunta al puerto 8000 (API news), no al gateway",
                )
            )
        elif url:
            checks.append(Check("gateway url", "ok", url))

    # 8. Host prerequisites ------------------------------------------------
    # The detail carries the very command that fixes it, so a red line in the
    # validation screen is always actionable.
    if report is not None:
        from .detect import prerequisites

        remedies = {item.key: item for item in prerequisites(report)}
        by_finding = {
            "Python": "python",
            "docker": "docker",
            "compose": "compose",
            "ffmpeg": "ffmpeg",
        }
        for finding in report.findings:
            if finding.status != "bad" or finding.name not in by_finding:
                continue
            severity = "bad"
            if finding.name in ("docker", "compose") and profile == "local":
                severity = "warn"  # a host-only run does not need compose
            item = remedies[by_finding[finding.name]]
            hint = f" · instala con: {item.remedy[0]}" if item.remedy else ""
            checks.append(Check(finding.name, severity, finding.detail + hint))
        if report.venvs:
            checks.append(Check("dependencias", "ok", "venv presente: " + ", ".join(report.venvs)))
        else:
            checks.append(Check("dependencias", "warn", "sin virtualenv: faltan dependencias"))

    return checks
