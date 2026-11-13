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
            " — unifica los tres valores (el asistente lo hace con «Unificar la clave del gateway»)"
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

    # 8. TTS engines, routing and device ----------------------------------
    if gateway is not None:
        from .settings import (
            CLOUD_ENGINES,
            ENGINE_INFO,
            MODEL_OPTIONS,
            config_requires_gpu,
            configured_engines,
            parse_engine_map,
            split_multi,
            valid_language_code,
        )

        engine = gateway.get("TTS_ENGINE").strip()
        if engine and engine not in {*ENGINE_INFO, "routed"}:
            checks.append(Check("tts engine", "bad", f"TTS_ENGINE desconocido: {engine}"))
        elif engine:
            checks.append(Check("tts engine", "ok", f"TTS_ENGINE={engine}"))

        extra = split_multi(gateway.get("TTS_EXTRA_ENGINES"))
        unknown_extra = [name for name in extra if name not in ENGINE_INFO]
        if unknown_extra:
            checks.append(
                Check(
                    "engines extra",
                    "bad",
                    "TTS_EXTRA_ENGINES contiene engines desconocidos: " + ", ".join(unknown_extra),
                )
            )
        elif extra and engine != "routed":
            checks.append(
                Check(
                    "engines extra",
                    "warn",
                    "TTS_EXTRA_ENGINES solo tiene efecto con TTS_ENGINE=routed",
                )
            )
        elif extra:
            checks.append(Check("engines extra", "ok", ", ".join(extra)))

        routing: dict[str, str] | None = None
        if engine == "routed":
            routing = parse_engine_map(gateway.get("TTS_ENGINES"))
            if routing is None:
                checks.append(Check("routed", "bad", "TTS_ENGINES no es un objeto JSON válido"))
            elif not routing:
                checks.append(Check("routed", "bad", "TTS_ENGINES está vacío"))
            else:
                unknown = sorted({value for value in routing.values() if value not in ENGINE_INFO})
                bad_languages = sorted(
                    code for code in routing if code != "_default" and not valid_language_code(code)
                )
                if unknown:
                    checks.append(
                        Check(
                            "routed",
                            "bad",
                            "TTS_ENGINES apunta a engines desconocidos: " + ", ".join(unknown),
                        )
                    )
                elif bad_languages:
                    checks.append(
                        Check(
                            "routed",
                            "bad",
                            "TTS_ENGINES usa claves de idioma inválidas: "
                            + ", ".join(bad_languages),
                        )
                    )
                else:
                    missing_default = "_default" not in routing
                    unsupported = [
                        f"{code}→{name}"
                        for code, name in routing.items()
                        if code != "_default"
                        and name in ENGINE_INFO
                        and "*" not in ENGINE_INFO[name].languages
                        and code not in ENGINE_INFO[name].languages
                    ]
                    detail = ", ".join(f"{code}→{name}" for code, name in routing.items())
                    if unsupported:
                        checks.append(
                            Check(
                                "routed",
                                "warn",
                                "posible idioma sin soporte: "
                                + ", ".join(unsupported)
                                + " · mapa: "
                                + detail,
                            )
                        )
                    elif missing_default:
                        checks.append(
                            Check(
                                "routed",
                                "warn",
                                "TTS_ENGINES no define _default; los idiomas no listados fallarán",
                            )
                        )
                    else:
                        checks.append(Check("routed", "ok", detail))

        selected = configured_engines(
            engine,
            gateway.get("TTS_ENGINES"),
            gateway.get("TTS_EXTRA_ENGINES"),
        )
        if config_requires_gpu(
            engine,
            gateway.get("TTS_ENGINES"),
            gateway.get("TTS_EXTRA_ENGINES"),
        ):
            device = gateway.get("TTS_DEVICE").strip() or "cuda:0"
            gpu_engines = [name for name in selected if name not in CLOUD_ENGINES]
            if not device.startswith("cuda"):
                checks.append(
                    Check(
                        "tts device",
                        "bad",
                        f"TTS_DEVICE={device} con engines GPU ("
                        + ", ".join(gpu_engines)
                        + "); usa cuda:0",
                    )
                )
            else:
                checks.append(Check("tts device", "ok", f"{device} para " + ", ".join(gpu_engines)))
            if report is not None:
                gpu = next((item for item in report.findings if item.name == "GPU"), None)
                if gpu is not None and gpu.status != "ok":
                    checks.append(
                        Check(
                            "gpu host",
                            "warn",
                            "hay engines GPU pero el host no detecta NVIDIA: " + gpu.detail,
                        )
                    )
        elif gateway.get("TTS_DEVICE").strip().startswith("cuda"):
            checks.append(
                Check(
                    "tts device",
                    "warn",
                    "TTS_DEVICE usa cuda pero no hay engines GPU seleccionados",
                )
            )

        if "fish_tts" in selected and not gateway.get("FISH_API_KEY").strip():
            checks.append(Check("fish tts", "warn", "fish_tts seleccionado sin FISH_API_KEY"))
        if "openai_tts" in selected and not gateway.get("OPENAI_TTS_BASE_URL").strip():
            checks.append(
                Check(
                    "openai tts",
                    "warn",
                    "openai_tts seleccionado sin OPENAI_TTS_BASE_URL",
                )
            )
        if "qwen3_tts" in selected:
            deploy_url = deploy.get("QWEN3_TTS_REMOTE_URL").strip() if deploy else ""
            if not (gateway.get("QWEN3_TTS_REMOTE_URL").strip() or deploy_url):
                checks.append(
                    Check(
                        "qwen3 tts",
                        "warn",
                        "qwen3_tts seleccionado sin QWEN3_TTS_REMOTE_URL",
                    )
                )
        if "omnivoice_remote" in selected and not gateway.get("OMNIVOICE_REMOTE_URL").strip():
            checks.append(
                Check(
                    "omnivoice remoto",
                    "warn",
                    "omnivoice_remote seleccionado sin OMNIVOICE_REMOTE_URL",
                )
            )

        downloads = split_multi(gateway.get("TTS_MODEL_DOWNLOADS"))
        cloud_downloads = [name for name in downloads if name.split(":", 1)[0] in CLOUD_ENGINES]
        unknown_downloads = [
            name for name in downloads if name not in MODEL_OPTIONS and name not in cloud_downloads
        ]
        if cloud_downloads:
            checks.append(
                Check(
                    "modelos",
                    "warn",
                    "los engines cloud no descargan pesos: " + ", ".join(cloud_downloads),
                )
            )
        elif unknown_downloads:
            checks.append(
                Check(
                    "modelos",
                    "bad",
                    "TTS_MODEL_DOWNLOADS contiene modelos desconocidos: "
                    + ", ".join(unknown_downloads),
                )
            )
        elif downloads:
            checks.append(Check("modelos", "ok", f"{len(downloads)} checkpoint(s) para precargar"))

    # 9. Host prerequisites ------------------------------------------------
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
