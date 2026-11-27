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

make engines                       # solo motores TTS: contenedores, GPU, API y términos
python3 scripts/setup.py --engines --plain  # misma ruta en modo línea
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
3. **Elige básica o personalizada** — la **básica** (recomendada) aplica un
   Docker Shape A con `TTS_DEVICE=cpu`, todos los engines TTS locales
   (pocket_tts, piper, kokoro y edgetts), genera las claves internas y pide la
   **URL y el modelo del LLM** externo, además de la **clave API** (opcional:
   puede quedar vacía con llama.cpp local). La **personalizada** te lleva al
   flujo completo de perfil, método y archivos.
4. **Confirma los términos de los engines** (perfil básico) — el paso «2b»
   lista las licencias de pocket_tts, piper y kokoro con su URL y exige
   aceptarlas explícitamente antes de seguir; `Continuar` no avanza mientras
   falte alguna. La aceptación se guarda en `ENGINE_TERMS_ACCEPTED`. En el
   perfil personalizado se confirman dentro de la sección de motores TTS.
5. **Elige el perfil** — Docker Shape A, desarrollo local, Docker Shape B o
   solo revisión; cada uno precarga sus valores por defecto sin pisar lo que
   ya hayas escrito a mano.
6. **Elige cómo instalar dependencias** — `uv sync` del workspace, un `.venv`
   por app con pip (`make venv`) o más tarde.
7. **Rellena los tres `.env`** — `deploy/.env`, `apps/news/.env` y
   `apps/gateway/.env` a partir de sus `.env.example`, sección por sección y
   con la explicación de cada variable. Los secretos se generan con `g`, la
   clave compartida del gateway se copia sola a `TTS_API_KEY` y `API_KEY`, y el
   hash bcrypt del administrador se crea desde su propio diálogo. El motor TTS
   (`routed`, `qwen3_tts`, `fish_tts`, `openai_tts`…), los sidecars y las
   credenciales cloud tienen su propia sección; `a` muestra las opciones
   avanzadas. Al cambiar los engines, el asistente recalcula
   `GATEWAY_UV_EXTRAS` para que la imagen Docker instale sus extras pip y
   sincroniza la infraestructura: añade `qwen3`/`omnivoice` a
   `COMPOSE_PROFILES`, escribe las URLs de los sidecars y pone `GATEWAY_GPU=1`
   cuando la selección necesita GPU. `make up` y `scripts/deploy.sh` añaden el
   overlay GPU solos; ejecuta `make up` para reconstruir la imagen. La clave del
   gateway es **un mismo valor en los tres archivos**: escribirla en cualquiera
   la sincroniza en las otras dos.
8. **Revisa el diff** antes de escribir nada; cada archivo se guarda con una
   copia `.bak-<fecha>`. Si las tres claves del gateway no coinciden, la
   revisión y la validación lo marcan como error y ofrecen
   **«Unificar la clave del gateway»** (evita los 401 en tiempo de ejecución).
9. **Valida** — valores obligatorios, claves coherentes entre archivos,
   plantillas sin rellenar, puertos, URL del gateway, requisitos del host y
   dependencias; cada línea roja incluye el comando que la arregla.
10. **Genera un informe sin secretos** — al guardar escribe
    `reports/setup-report.md` con host, método, validación, configuración no
    sensible, motores/voces/modelos TTS y colecciones (destinatarios
    redactados). Para adjuntarlo a un issue:
    `./scripts/upload_setup_report.sh` (o `--issue N` para comentar).

`make engines` abre directamente la parte de motores TTS y guarda **solo**
`deploy/.env` y `apps/gateway/.env` (no toca `news/.env` ni instala
dependencias). Revisa contenedores y GPU, pide las credenciales obligatorias
(Fish, OpenAI-compatible, HuggingFace cuando aplica) y confirma los términos de
cada modelo, igual que el paso 4 del perfil básico. La aceptación queda
registrada en `ENGINE_TERMS_ACCEPTED`, pero los repos gated de HuggingFace
siguen necesitando el botón **Agree and access repository** en la web antes de
que `HF_TOKEN` funcione.

La entrada **Crear voces diseñadas básicas** (o `python3 scripts/setup.py
--design-voices`) lee `config/basic_voices.toml` y crea cada voz en dos sitios:
como preset de `POST /api/v1/voices/design` del gateway y como voz generada de
news vía `POST /api/admin/voices/custom`, con `run_in_modal=false`. Es
idempotente: si el nombre ya existe en el gateway o en news lo respeta. El
engine debe declarar `instruct_voice_design` en `config/engines.toml`; el
manifiesto trae cuatro ejemplos es/en. Usa `--design-dry-run` para solo
previsualizar y `--basic-voices PATH` para otro manifiesto.

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

Al arrancar, la TUI abre una **guía paginada de valores externos** (`←/→` o
`PgUp/PgDn` para cambiar de página) con qué credenciales necesita cada proveedor,
las URLs para obtenerlas y las acciones concretas: Fish Audio, HuggingFace
(términos del repo gated y token), Telegram, SMTP/Gmail, LLM y TTS
OpenAI-compatible. Se puede saltar al menú con `Esc` o desde el botón
correspondiente, y reabrir desde **«Guía de valores externos»**.

Teclas de la TUI: `↑/↓` o `Tab` mover · `Enter` seleccionar/editar ·
`Esc` atrás · `Espacio` alternar sí/no · `←/→` cambiar opciones ·
`g` generar secreto · `a` opciones avanzadas · `Ctrl+R` valor por defecto ·
`F1`/`?` ayuda · `F10`/`q` salir. Al editar, `Tab` guarda y pasa al siguiente
campo, y `Shift+Tab` guarda y vuelve al anterior. En las pantallas de `.env`,
`F1` abre la referencia detallada de cada campo (obligatorio/opcional, valor
efectivo, opciones y valor por defecto). Cada campo muestra `[interno]` si lo
resuelve el stack (puertos, TTS, voces, claves compartidas) o `[externo]` si
requiere un proveedor/API externa (LLM, Telegram, SMTP, Fish, OpenAI, HF).
«Guardar en disco y continuar» escribe el archivo antes de pasar al siguiente.

La TUI usa un único tema fijo inspirado en el instalador Debian: fondo azul,
panel claro, texto de alto contraste y selección roja. Para terminales sin
color o de alto contraste se puede usar `NEWSVOICE_TUI_THEME=mono`; cualquier
otro valor cae en el tema Debian.

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
