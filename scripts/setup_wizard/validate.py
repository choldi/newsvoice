"""Configuration validation shared by the TUI and the CLI fallback."""

from __future__ import annotations

import json
from dataclasses import dataclass

from engine_runtime import MODES as RUNTIME_MODES
from engine_runtime import EngineRuntimeError

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
    code: str = ""
    fixable: bool = False


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
        seen: set[str] = set()
        duplicates: list[str] = []
        for entry in env.entries():
            if entry.key is None or entry.commented:
                continue
            if entry.key in seen and entry.key not in duplicates:
                duplicates.append(entry.key)
            seen.add(entry.key)
        if duplicates:
            checks.append(
                Check(
                    "claves duplicadas",
                    "warn",
                    f"{key}/.env repite: {', '.join(sorted(duplicates))}",
                    code=f"duplicates:{key}",
                    fixable=True,
                )
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

    # 2b. AI repair endpoint is optional but must be complete ---------------
    news = envs.get("news")
    if news is not None:
        ai_base = news.get("AI_FIX_API_BASE").strip()
        ai_model = news.get("AI_FIX_MODEL").strip()
        if bool(ai_base) != bool(ai_model):
            checks.append(
                Check(
                    "solventar con IA",
                    "warn",
                    "AI_FIX_API_BASE y AI_FIX_MODEL deben definirse juntos",
                )
            )
        elif ai_base and not ai_base.rstrip("/").endswith("/v1"):
            checks.append(Check("solventar con IA", "warn", "AI_FIX_API_BASE debería incluir /v1"))
        elif ai_base and ai_model:
            checks.append(Check("solventar con IA", "ok", "endpoint de diagnóstico configurado"))

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
        fix = " — usa «Solucionar todo» o la reparación de secretos internos"
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
            MANAGED_SIDECAR_PROFILES,
            MODEL_OPTIONS,
            canonical_engine,
            config_requires_gpu,
            configured_engines,
            engine_modes,
            engine_terms,
            gateway_docker_extras,
            parse_engine_map,
            parse_runtime_modes,
            render_runtime_modes,
            required_engine_keys,
            runtime_plan,
            runtime_specs,
            selected_engine_infos,
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

        valid_engines = {
            name
            for name, spec in runtime_specs().items()
            if spec.selectable and spec.gateway_engine
        }
        routing_values = (parse_engine_map(gateway.get("TTS_ENGINES")) or {}).values()
        referenced = [
            engine,
            *routing_values,
            *extra,
            *parse_runtime_modes(gateway.get("TTS_RUNTIME_MODES")),
        ]
        invalid_engines = sorted(
            {
                canonical_engine(name)
                for name in referenced
                if name and name != "routed" and canonical_engine(name) not in valid_engines
            }
        )
        if invalid_engines:
            checks.append(
                Check(
                    "engines configurables",
                    "bad",
                    "IDs no configurables en el gateway: " + ", ".join(invalid_engines),
                    code="invalid-ids",
                    fixable=True,
                )
            )

        raw_modes_text = gateway.get("TTS_RUNTIME_MODES").strip()
        try:
            raw_modes = json.loads(raw_modes_text or "{}")
        except (TypeError, ValueError):
            raw_modes = None
        modes = parse_runtime_modes(raw_modes_text)
        if raw_modes is None or not isinstance(raw_modes, dict):
            checks.append(
                Check("modos de carga", "bad", "TTS_RUNTIME_MODES no es un objeto JSON válido")
            )
            raw_modes = {}
        else:
            unknown_modes = sorted(
                {
                    canonical_engine(str(name))
                    for name in raw_modes
                    if canonical_engine(str(name)) not in ENGINE_INFO
                }
            )
            unsupported_modes = sorted(
                {
                    f"{canonical_engine(str(name))}:{str(mode).strip().lower()}"
                    for name, mode in raw_modes.items()
                    if str(mode).strip().lower() in RUNTIME_MODES
                    and str(mode).strip().lower() not in engine_modes(canonical_engine(str(name)))
                }
            )
            invalid_values = sorted(
                {
                    str(mode).strip().lower()
                    for mode in raw_modes.values()
                    if str(mode).strip().lower() not in RUNTIME_MODES
                }
            )
            if unknown_modes or unsupported_modes or invalid_values:
                problems = [
                    *(
                        [f"engines desconocidos: {', '.join(unknown_modes)}"]
                        if unknown_modes
                        else []
                    ),
                    *(
                        [f"modos no soportados: {', '.join(unsupported_modes)}"]
                        if unsupported_modes
                        else []
                    ),
                    *([f"modos inválidos: {', '.join(invalid_values)}"] if invalid_values else []),
                ]
                checks.append(Check("modos de carga", "bad", " · ".join(problems)))
            else:
                checks.append(
                    Check(
                        "modos de carga",
                        "ok",
                        render_runtime_modes(modes) or "sin perfiles de modo explícitos",
                    )
                )

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
        try:
            runtime_resolution = runtime_plan(selected, modes)
        except EngineRuntimeError as exc:
            checks.append(
                Check(
                    "conflictos de runtime",
                    "bad",
                    str(exc),
                    code="runtime-conflict",
                    fixable=True,
                )
            )
            runtime_resolution = None
        if runtime_resolution is not None:
            if runtime_resolution.conflicts:
                checks.append(
                    Check(
                        "conflictos de runtime",
                        "bad",
                        " · ".join(runtime_resolution.conflicts),
                        code="runtime-conflict",
                        fixable=True,
                    )
                )
            else:
                checks.append(
                    Check(
                        "conflictos de runtime",
                        "ok",
                        render_runtime_modes(runtime_resolution.modes),
                    )
                )
        docker_extras = deploy.get("GATEWAY_UV_EXTRAS").strip() if deploy else ""
        expected_extras = gateway_docker_extras(
            engine,
            gateway.get("TTS_ENGINES"),
            gateway.get("TTS_EXTRA_ENGINES"),
            modes,
        )
        if not docker_extras:
            checks.append(
                Check(
                    "docker extras",
                    "warn",
                    "GATEWAY_UV_EXTRAS vacío: la imagen no incluirá los engines "
                    "seleccionados; guarda el setup y ejecuta make up",
                )
            )
        elif docker_extras != expected_extras:
            checks.append(
                Check(
                    "docker extras",
                    "warn",
                    "GATEWAY_UV_EXTRAS no coincide con la selección TTS; "
                    "vuelve a guardar el setup y ejecuta make up",
                )
            )
        else:
            checks.append(Check("docker extras", "ok", expected_extras))

        if config_requires_gpu(
            engine,
            gateway.get("TTS_ENGINES"),
            gateway.get("TTS_EXTRA_ENGINES"),
            modes,
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

        selected_infos = selected_engine_infos(
            engine,
            gateway.get("TTS_ENGINES"),
            gateway.get("TTS_EXTRA_ENGINES"),
            modes,
        )
        expected_profiles = sorted(
            runtime_resolution.profiles if runtime_resolution is not None else ()
        )
        required_urls = dict(runtime_resolution.urls) if runtime_resolution is not None else {}
        if not expected_profiles:
            expected_profiles = list(
                dict.fromkeys(profile for info in selected_infos for profile in info.profiles)
            )
        profiles = split_multi(deploy.get("COMPOSE_PROFILES")) if deploy else []
        missing_profiles = [name for name in expected_profiles if name not in profiles]
        stale_profiles = [
            name
            for name in profiles
            if name in MANAGED_SIDECAR_PROFILES and name not in expected_profiles
        ]
        if missing_profiles:
            checks.append(
                Check(
                    "perfiles engines",
                    "warn",
                    "faltan perfiles Compose para los sidecars: "
                    + ", ".join(missing_profiles)
                    + "; ejecuta make engines y make up",
                )
            )
        elif stale_profiles:
            checks.append(
                Check(
                    "perfiles engines",
                    "warn",
                    "perfiles de sidecar sin engine seleccionado: "
                    + ", ".join(stale_profiles)
                    + "; make engines los limpia",
                )
            )
        elif expected_profiles:
            checks.append(Check("perfiles engines", "ok", ", ".join(expected_profiles)))

        if expected_profiles:
            missing_runtime_profiles = [name for name in expected_profiles if name not in profiles]
            checks.append(
                Check(
                    "perfiles de runtime",
                    "warn" if missing_runtime_profiles else "ok",
                    "faltan perfiles: " + ", ".join(missing_runtime_profiles)
                    if missing_runtime_profiles
                    else ", ".join(expected_profiles),
                )
            )
        else:
            checks.append(Check("perfiles de runtime", "ok", "sin sidecars requeridos"))

        missing_urls = [
            f"{file_key}.{key}"
            for key in sorted(required_urls)
            for file_key in ("deploy", "news", "gateway")
            if file_key in envs and not envs[file_key].get(key).strip()
        ]
        checks.append(
            Check(
                "urls de sidecar",
                "warn" if missing_urls else "ok",
                "faltan URLs: " + ", ".join(missing_urls)
                if missing_urls
                else (", ".join(sorted(required_urls)) or "sin sidecars requeridos"),
            )
        )

        if deploy is not None:
            gpu_flag = deploy.get("GATEWAY_GPU").strip() or "0"
            if config_requires_gpu(
                engine,
                gateway.get("TTS_ENGINES"),
                gateway.get("TTS_EXTRA_ENGINES"),
                modes,
            ):
                if gpu_flag != "1":
                    checks.append(
                        Check(
                            "gpu overlay",
                            "warn",
                            "la selección necesita GPU pero GATEWAY_GPU != 1; "
                            "make up no añadirá el overlay",
                        )
                    )
                else:
                    checks.append(Check("gpu overlay", "ok", "GATEWAY_GPU=1"))
            elif gpu_flag == "1":
                checks.append(
                    Check(
                        "gpu overlay",
                        "warn",
                        "GATEWAY_GPU=1 sin engines GPU seleccionados",
                    )
                )

        for info in selected_infos:
            if not info.docker_supported:
                checks.append(
                    Check(
                        f"{info.key} docker",
                        "warn",
                        f"{info.key} no está en la imagen Docker; {info.manual_install}",
                    )
                )
            if not info.remote_url:
                continue
            deploy_url = (
                deploy.get(info.deploy_url_key).strip()
                if deploy is not None and info.deploy_url_key
                else ""
            )
            gateway_url = gateway.get(info.gateway_url_key).strip() if info.gateway_url_key else ""
            if info.key == "qwen3_tts":
                if not (gateway_url or deploy_url):
                    checks.append(
                        Check(
                            "qwen3 tts",
                            "warn",
                            "qwen3_tts seleccionado sin QWEN3_TTS_REMOTE_URL",
                        )
                    )
                elif not deploy_url:
                    checks.append(
                        Check(
                            "qwen3 tts",
                            "warn",
                            "falta deploy/QWEN3_TTS_REMOTE_URL: Docker no puede "
                            "inyectar la URL del sidecar",
                        )
                    )
                else:
                    checks.append(Check("qwen3 tts", "ok", deploy_url))
            elif info.key == "omnivoice_remote" and not gateway_url:
                checks.append(
                    Check(
                        "omnivoice remoto",
                        "warn",
                        "omnivoice_remote seleccionado sin OMNIVOICE_REMOTE_URL",
                    )
                )

        for key in required_engine_keys(
            engine,
            gateway.get("TTS_ENGINES"),
            gateway.get("TTS_EXTRA_ENGINES"),
            modes,
        ):
            if key == "FISH_API_KEY" and not gateway.get(key).strip():
                checks.append(Check("fish tts", "warn", "fish_tts seleccionado sin FISH_API_KEY"))
            elif key == "OPENAI_TTS_BASE_URL" and not gateway.get(key).strip():
                checks.append(
                    Check(
                        "openai tts",
                        "warn",
                        "openai_tts seleccionado sin OPENAI_TTS_BASE_URL",
                    )
                )

        if deploy is not None:
            accepted = split_multi(deploy.get("ENGINE_TERMS_ACCEPTED"))
            terms = engine_terms(
                engine,
                gateway.get("TTS_ENGINES"),
                gateway.get("TTS_EXTRA_ENGINES"),
                modes,
            )
            missing_terms = [info.label for info in terms if info.key not in accepted]
            if missing_terms:
                checks.append(
                    Check(
                        "términos engines",
                        "warn",
                        "faltan términos por aceptar: " + ", ".join(missing_terms),
                    )
                )
            elif terms:
                checks.append(
                    Check("términos engines", "ok", ", ".join(info.label for info in terms))
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
