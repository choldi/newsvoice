# newsvoice

Monorepo que unifica **news-engine** (`apps/news`) y **tts-api / gateway**
(`apps/gateway`) en una única solución desplegable en **1..N servidores** por
configuración (overlays de compose), no por fork.

Este repositorio se creó importando las dos historias completas con
`git subtree add` (sin `--squash`): `git log --follow` conserva la procedencia
por fichero.

## Layout

```
newsvoice/
├── apps/
│   ├── news/       # ← jordi/news-summary (news-engine)
│   └── gateway/    # ← choldi/ov_api (tts-api)
├── libs/
│   └── tts_contract/   # modelos Pydantic compartidos (P4)
├── deploy/             # compose unificado + overlays (P2)
└── scripts/            # ops fusionados (P7) + setup.py (asistente guiado)
```

## Instalación guiada

`scripts/setup.py` es un asistente con TUI que prepara el entorno desde un
clone limpio. Solo usa la biblioteca estándar de Python 3.11+, así que funciona
**antes** de instalar nada:

```bash
python3 scripts/setup.py           # TUI (terminal interactiva)
python3 scripts/setup.py --plain   # modo línea (sin TUI o salida redirigida)
python3 scripts/setup.py --check   # solo detectar y validar, no escribe nada
make setup                         # lo mismo desde make
```

Uso sin interacción (para scripts o CI):

```bash
python3 scripts/setup.py --profile docker --install uv --yes
python3 scripts/setup.py --profile audit --check
```

Pasos del asistente:

1. **Detecta el entorno** — Python, uv, docker, ffmpeg, GPU, puertos ocupados,
   `.env` existentes y virtualenvs.
2. **Revisa los requisitos** — si falta algo (docker, plugin compose, ffmpeg,
   uv, virtualenvs…) aparece el paso **0 · requisitos**: qué se rompe sin él y
   el comando exacto para instalarlo, con un botón que lo ejecuta por ti
   (los pasos con `sudo` se te dejan copiados por si prefieren correrlos a mano).
3. **Elige el perfil** — Docker Shape A, desarrollo local, Docker Shape B o
   solo revisión; cada uno precarga sus valores por defecto sin pisar lo que
   ya hayas escrito a mano.
4. **Elige cómo instalar dependencias** — `uv sync` del workspace, un `.venv`
   por app con pip (`make venv`) o más tarde.
5. **Rellena los tres `.env`** — `deploy/.env`, `apps/news/.env` y
   `apps/gateway/.env` a partir de sus `.env.example`, sección por sección y
   con la explicación de cada variable. Los secretos se generan con `g`, la
   clave compartida del gateway se copia sola a `TTS_API_KEY` y `API_KEY`, y el
   hash bcrypt del administrador se crea desde su propio diálogo. El motor TTS
   (`routed`, `qwen3_tts`, `fish_tts`, `openai_tts`…), los sidecars y las
   credenciales cloud tienen su propia sección; `a` muestra las opciones
   avanzadas. La clave del gateway es **un mismo valor en los tres archivos**:
   escribirla en cualquiera las sincroniza en las otras dos.
6. **Revisa el diff** antes de escribir nada; cada archivo se guarda con una
   copia `.bak-<fecha>`. Si las tres claves del gateway no coinciden, la
   revisión y la validación lo marcan como error y ofrecen
   **«Unificar la clave del gateway»** (evita los 401 en tiempo de ejecución).
7. **Valida** — valores obligatorios, claves coherentes entre archivos,
   plantillas sin rellenar, puertos, URL del gateway, requisitos del host y
   dependencias; cada línea roja incluye el comando que la arregla.

Además del flujo guiado, desde la pantalla inicial también puedes:

- **Editar los `.env` ya configurados** — abre cualquiera de los tres archivos
  para cambiar cualquier valor (incluidos secretos y desplegables), ver el diff
  pendiente y guardarlo en disco directamente (copia `.bak-<fecha>` incluida).
- **Importar valores desde un archivo `.env`** — lee cualquier `KEY=VALUE`
  (acepta comillas y `export`) y aplica a los tres archivos solo las claves que
  ya existen en sus plantillas; las claves sin destino se listan e ignoran.
- **Exportar la configuración** — vuelca los valores activos de los tres `.env`
  en un único archivo agrupado por sección (por defecto `.env.newsvoice`),
  útil para replicar la instalación. Atención: el resultado incluye secretos en
  claro. El asistente se niega a exportar encima de los `.env` del proyecto.

Teclas de la TUI: `↑/↓` mover · `Enter` editar/abrir · `Esc` atrás ·
`g` generar secreto · `a` opciones avanzadas · `Ctrl+R` valor por defecto ·
`t`/`F2` cambiar tema · `?` ayuda · `q` salir.

El tema por defecto es `auto`: detecta fondo claro u oscuro con `COLORFGBG` y
elige la paleta adecuada. `t`/`F2` rota entre `auto`, `oscuro`, `claro` y
`monocromo` (alto contraste sin depender del color). También se puede fijar el
tema inicial con `NEWSVOICE_TUI_THEME=dark|light|mono`.

La lógica vive en `scripts/setup_wizard/` (std-lib only) y está cubierta por
`apps/news/tests/unit/test_setup_wizard.py`.

## Desarrollo

El workspace se instala en un único `.venv` raíz:

```bash
uv sync --all-packages                # deps base + grupos dev de news, gateway y tts_contract
uv sync --all-packages --all-extras   # añade todos los motores TTS (torch/CUDA)
```

La raíz está declarada como proyecto virtual (`[tool.uv] package = false`), así
que `uv sync` no intenta empaquetar `newsvoice`. Si prefieres un `.venv` por app,
`make venv` sigue disponible.

Puertas de calidad (lo mismo que corre CI):

```bash
make lint          # ruff check en news, gateway, tts_contract y scripts
make format-check  # ruff format --check
make test          # pytest de news + gateway
make check         # lint + format-check + test
```

`make help` lista el resto de objetivos (compose, deploy, schedules, logs…).

`make report` genera `deploy-report.md` en Markdown, pensado para pegarlo en
una herramienta AI: índice de hallazgos (`❌` crítico / `⚠️` aviso) y un
**análisis en profundidad** que empareja cada problema con su causa raíz, su
impacto, la solución y los comandos exactos (requisitos del host, coherencia
de claves, sidecars qwen3/omnivoice, LLM, secretos de plantilla…).

## Seguridad

Los escáneres viven en un virtualenv aislado `.sec-venv` (ignorado por git) para
no contaminar el entorno de desarrollo. `trivy` es un binario Go que se descarga
automáticamente la primera vez.

```bash
make security-venv     # crea .sec-venv y descarga trivy
make security-scan     # pip-audit + trivy + checkov
make security-scan-ci  # igual, pero falla si hay hallazgos
```

Scanners disponibles (seleccionables con `SCANNERS="..."`):

| Scanner | Qué revisa |
|---|---|
| `pip-audit` | dependencias Python de news y gateway (vía `uv export`) |
| `trivy` | imágenes Docker `newsvoice-api` y `newsvoice-gateway` |
| `checkov` | `deploy/` (compose/dockerfiles) y secretos |
| `semgrep` | SAST sobre `apps/` |

Ejemplo:

```bash
make security-scan SCANNERS="pip-audit semgrep"
```

`trivy` escanea imágenes locales, así que constrúyelas antes con `make up` en el
host donde existan.

## Estado

En construcción según `apps/news/MERGE_PLAN.md` (fases P0–P10). Remotos
originales (`jordi/news-summary`, `choldi/ov_api`) pasan a ser archivos de solo
lectura.
