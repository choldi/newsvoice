# MERGE_PLAN.md — Unificar `news-engine` y `tts-api` en un único repo

**Fecha:** 2026-10-16
**Estado:** plan aprobado; **P0** (2026-10-16), **P1** (2026-10-17), **P2**
(compose, 2026-10-17), **P3** (engines, 2026-10-17) y **P4** (contrato, 2026-10-17)
completados. El **cutover en
debwk se difiere a P10**. Pendiente **P5**.
**Objetivo:** un solo repo, una sola solución, desplegable en **1..N servidores** por
configuración (no por fork), con la historia de ambos repos preservada.

Este plan es el paraguas de **P0–P10**. Cuando entre en conflicto con otro documento,
manda este (para el merge) y `CONVENTIONS.md`/`Phases.md` para el estilo.

---

## 0. Punto de partida verificado

| | news-engine | tts-api (gateway) |
|---|---|---|
| Path | `~/aider_projects/news_sumary` | `~/aider_projects/omni_api` |
| Remoto | Gitea `jordi/news-summary` (`main` @ `cc28af5`) | GitHub `choldi/ov_api` (PR **#1** abierto) |
| Paquete | `news-engine` 0.4.0, `src/`, py3.11 | `tts-api` 0.2.0, `tts_api/` en raíz, py3.11–3.12, `uv.lock` |
| Runtime | 11 servicios compose + Temporal + Postgres, SQLite `data/news.db` | **systemd `tts-api.service` en :8001**, venv `venv-omnivoice`, `TTS_ENGINE=routed` |
| Docker | multi-stage `python:3.11-slim` + ffmpeg | `python:3.12-slim` + libsndfile1 + ffmpeg + reserva nvidia (**sin usar hoy**) |

Tres hechos que condicionan todo el plan:

1. **debwk es a la vez la app host y la GPU box** (`Deploy.md` §8.3). El shape
   "multi-servidor" real hoy: debwk corre todo, Modal en la nube, `winsrv` como LLM
   fallback y `vm-ubuntu` parada.
2. **El gateway no está containerizado** a pesar de tener Dockerfile/compose — esos
   mapean `:8000`, mientras systemd sirve en `:8001` y `TTS_API_BASE_URL` espera 8001.
   Hay que unificarlo.
3. **`omni_api` arrastra basura trackeada** que envenenaría el monorepo si se importa
   tal cual (verificado): `MagicMock/**` (16 WAVs filtrados desde un test cuyo
   `VOICES_DIR` era un `MagicMock`), `storage/omnivoice.db` + un WAV de voz clonada,
   `tts_api.egg-info/**` (5), `mensaje*.txt` (7), y `uv.lock` **a la vez trackeado y
   gitignored**. `news_sumary` está limpio (solo `data/.gitkeep`).

**Contrato realmente usado por news-engine** (debe sobrevivir sin cambios):
`/api/v1/health`, `/disk`, `/tts`, `/conversations`, `/voices/stock`,
`/voices/cloned`, `/voices/clone`. El doc-TTS sigue en **Edge-only** (decisión de
usuario). Modal se mantiene (Zonos2 / VoxCPM2 / LLM judge); `MODAL_ENABLED` sigue
sin activar en producción.

---

## 1. Goals / non-goals

**Goals** — un repo; un entrypoint de deploy; desplegable en **1..N servidores** por
configuración, no por fork; ambas historias preservadas; **cero cambio de
comportamiento** durante la importación.

**Non-goals** — features nuevas, sustituir Modal, multi-user, rediseñar la API, y
programación de GPU más allá de lo que `design_window.sh` ya hace (eso es P8).

---

## 2. Repo y layout objetivo

Gitea `http://192.168.5.6:3000/jordi/newsvoice.git` (nombre libre; `news-summary`
se conserva). Workspace **uv** para que P4 entregue un paquete compartido real:

```
newsvoice/
├── README.md  AGENTS.md  Architecture.md  Deploy.md  CONVENTIONS.md  Phases.md  PENDING.md
├── pyproject.toml                 # raíz uv workspace ([tool.uv.workspace] members)
├── uv.lock                        # un lock para toda la solución
├── Makefile                       # unificado: up/down/status/check/test/deploy
├── apps/
│   ├── news/                      # ← news_sumary (subtree, historia completa)
│   │   ├── pyproject.toml  Dockerfile  src/  tests/
│   │   ├── collections.toml(.example)  voice_profiles.json
│   └── gateway/                   # ← omni_api (subtree, historia completa)
│       ├── pyproject.toml  Dockerfile  tts_api/  tests/
├── libs/
│   └── tts_contract/              # P4: modelos Pydantic In/Out compartidos
├── deploy/
│   ├── docker-compose.yml         # la solución única: todos los servicios
│   ├── docker-compose.gpu.yml     # overlay: reserva nvidia para el gateway
│   ├── docker-compose.split.yml   # overlay: multi-host (gateway-only / news-only)
│   └── .env.example
├── scripts/                       # ops fusionados (deploy.sh, status.sh,
│                                  #   create_namespaces.sh, design_window.sh,
│                                  #   modal_cost.py, benchmark_*.py …)
└── .github/workflows/ci.yml       # jobs por app
```

---

## 3. Decisiones cerradas

| # | Decisión | Consecuencia |
|---|---|---|
| 1 | **uv workspace** — `apps/news`, `apps/gateway`, `libs/tts_contract` | P4 pasa a ser un paquete real que importan las dos apps; un `uv.lock`; ruff/mypy/pytest en raíz |
| 2 | **Container + nvidia toolkit** para el gateway | un solo entrypoint de deploy; publicado como `8001:8000`. **Verificado 2026-10-17:** el toolkit (1.20.1) y el spec CDI ya existen en debwk → **no hace falta root**; `docker run --gpus all` expone `/dev/nvidia*`. La containerización efectiva se difiere a **P10** (ver §10) |
| 3 | **Historia completa, sin `--squash`** | `git log --follow` conserva la procedencia por fichero; la purga de basura debe aterrizar **antes** del subtree |
| 4 | **py3.11 + ruleset de ruff del gateway**, `fail_under=80` mantenido, `mypy --strict` aplazado a P9 | las imágenes de news-engine siguen en 3.11; el Dockerfile del gateway baja de 3.12 a 3.11; la matriz de CI colapsa a una versión |

---

## 4. Importación de historia (P1)

```bash
git init newsvoice && cd newsvoice
git remote add news     http://192.168.5.6:3000/jordi/news-summary.git
git remote add gateway  https://github.com/choldi/ov_api.git
git subtree add --prefix=apps/news     news    main      # sin --squash
git subtree add --prefix=apps/gateway  gateway main      # sin --squash
```

- **Sin `--squash`** → ambas historias completas quedan alcanzables
  (`git log --follow` funciona por fichero) y `git push` a Gitea las lleva todas.
- **Precondición:** PR #1 mergeado en `choldi/ov_api/main` (3 commits locales sin
  push) **y** la purga de basura §7 aterrizar en la rama del gateway primero.
- Los remotos viejos pasan a ser archivos de solo lectura.

---

## 5. Modelo de despliegue — una solución, 1..N servidores

El número de servidores es una **elección de overlay de compose**, nunca un stack
distinto.

**Shape A — un solo host (debwk hoy):** `make up` → los 11 servicios de news + `gateway`
en un único proyecto. El gateway se resuelve como `http://gateway:8001/api/v1`.

**Shape B — app host + GPU host:** la GPU host solo corre `gateway` (+ la unidad
systemd de diseño de voces); la app host corre el resto;
`TTS_API_BASE_URL=http://<gpu-host>:8001/api/v1` (hoy el nombre Tailscale). Temporal
se queda en la app host; `VOICE_STAGING_URL` apunta a la app host.

**Shape C — borde en la nube:** Modal sigue siendo backend tras el mismo dispatch.

**Contrato de puertos (hay que arreglarlo):** el contenedor del gateway escucha en
8000 por dentro y se **publica como `8001:8000`**, casando con `TTS_API_BASE_URL`, la
comprobación `GET /disk` del monitor y el 8001 actual de systemd. En Shape B,
publicación directa al 8001 del host.

**Prerrequisito GPU — ya satisfecho:** `nvidia-container-toolkit` 1.20.1 y el spec
CDI ya están en debwk (verificado 2026-10-17 con `docker run --gpus all`, que expone
`/dev/nvidia*`); **no hace falta root ni registrar el runtime `nvidia`**.

**Pendiente para P10 (no P2):** decidir cómo obtiene los engines el contenedor —
imagen con los extras (`pocket_tts`, etc.) o montar el venv externo
(`~/ov_api/venv-omnivoice`, py3.13). Y migrar el código desplegado (línea pre-rename
`omnivoice_api` + `OMNIVOICE_*`, ver §10) al `tts_api`/`TTS_*` de `main`.

---

## 6. Fases (entregable + aceptación)

| Fase | Trabajo | Aceptación |
|---|---|---|
| **P0** Reconciliar y congelar | Purgar la basura (§7); diff del `dd874e3` desplegado (paquete `omnivoice_api`, engine real `qwen3_tts`) vs local; recuperar el commit del engine `qwen3_tts`; backup en debwk de `data/`, `storage/`, `.env`s | ✅ `git log` sin `MagicMock/` ni `*.egg-info`; tarball de backup en debwk; `dd874e3` **recuperado** en el repo del gateway como ref `deployed/main` (queda aquí, **no en `main`**). **Decisión:** portarlo a `main` (rename `omnivoice_api`→`tts_api` + conflictos) es **P3**, no P0 |
| **P1** Importar historias | subtree adds de arriba; raíz del workspace + docs de AGENTS/Phases fusionados | Ambas historias preservadas (149 gateway + 240 news commits alcanzables). `git log --follow 003d584^2 -- tts_api/main.py` alcanza la historia pre-rename (subtree: --follow desde HEAD no cruza el prefijo `apps/gateway/` por limitación de git; historia 100% conservada). |
| **P2** Un solo compose + GPU | ✅ `deploy/docker-compose.yml` (+ overlays gpu/split) + `deploy/.env.example`; servicio `gateway`; contrato `8001:8000`; proyecto `newsvoice` | ✅ validado con `docker compose config` (11 servicios + gateway; split `news`=10 / `gateway`=1) y GPU-in-container verificado en debwk. **El Shape A verde en debwk se difiere a P10** (downtime + port del engine) |
| **P3** Engines | ✅ portar `qwen3_tts` (desde `dd874e3`) a `apps/gateway` renombrado, con catálogo `GET /api/v1/engines` (`locations`) y override `engine` en `/tts`; **P3b Piper + P3c Chatterbox ya hechos**. `voxcpm2`/`zonos2` **diferidos** (Modal-only, sin runtime local en debwk) | ✅ `ruff check` + `ruff format` limpios y **378 tests verdes**; `qwen3_tts` accesible vía `TTS_ENGINE` y por pinning en `routed`; los gates de benchmark siguen siendo la autoridad |
| **P4** `libs/tts_contract` | ✅ `libs/tts_contract` **0.1.0** con `errors`, `voices`, `engines`, `system` y `requests`; el gateway declara `response_model` con ellos y news-engine parsea/escribe con ellos | ✅ news-engine ya no parsea JSON del gateway a mano (`tts_backends`, `narration_tts`, `voices`, `monitoring/checks`); gates **1510** (news) + **380** (gateway) tests verdes, ruff limpio |
| **P5** Consolidación de dispatch | ✅ extraído `src/api/services/gateway.py` (dispatcher único) sobre `tts_contract`; `tts_backends`, `narration_tts` y `voices` usan el dispatcher; conservar Edge por defecto, kill switch `TTS_API_ENABLED` y la regla `410 Gone` | ✅ **148 passed** (gateway + afectados) + **1523 passed** (news full); ruff limpio; se mantiene el 80% de cobertura; digests byte-idénticos en `edge` |
| **P6** Catálogo de voces → gateway | Mover `voice_profiles.json` (y fuentes/manifest del runtime `data/voices/*`) al gateway (`apps/gateway/data/voices/` o `apps/gateway/` según diseño). News-engine debe leer catálogo vía gateway (`GET /admin/voices/catalog` o `GET /api/v1/voices/stock` + catálogo) en lugar de filesystem local; conservar compatibilidad. | `GET /admin/voices/catalog` servido desde gateway; news-engine consume catálogo remoto; `data/voices` gitignored en ambos sitios y export/import cubierto por P9.5 |
| **P7** Ops unificados | un `Makefile`, `scripts/` fusionados, `make status` cubre ambas apps | `make status` reporta los 12 servicios + salud del gateway |
| **P8** Lease de GPU | formalizar `design_window.sh` como lease explícito para que síntesis y diseño no colisionen | test del lease + tabla `tts_leases` extendida |
| **P9** Docs + CI | Deploy.md reescrito para Shapes A/B; jobs de CI por app; arreglar `mypy tts_api` (133 errores) | CI verde en el monorepo |
| **P9.5** Export/Import de configuración y voces | Crear `scripts/export_config.sh` y `scripts/import_config.sh` (volcar `apps/news/.env.example` + `deploy/.env.example`, `collections.toml`, `data/voices/manifest.json`, `data/voices/voice_profiles.json`, secretos requeridos documentados). Verificar export en local y producir tarball `newsvoice-config-<ts>.tar.gz` listo para import en el nuevo monorepo | Export generado, checksum, tarball en `backups/` o path documentado, y probado `import_config.sh --dry-run`; rutas del gateway actualizadas para leer catálogo desde `apps/gateway/data/voices/` o path unificado |
| **P10** Cutover | desplegar el monorepo en debwk, retirar los checkouts `~/news-summary` y `~/ov_api`, archivar remotos | `make status` verde; digest diario entregado |

---

## 7. Basura a purgar antes de importar (obligatorio)

En `omni_api` (verificado con `git ls-files`):

- `MagicMock/**` — 16 WAVs filtrados por un test con `VOICES_DIR` mockeado
- `storage/omnivoice.db` + `storage/voices/**/reference.wav` (DB y datos de voz)
- `tts_api.egg-info/**` (5 ficheros)
- `mensaje*.txt` (7 ficheros de notas)
- `uv.lock` trackeado **y** gitignored → **decidido: se trackea** (el Dockerfile usa
  `uv sync --frozen` y el workspace lo necesita); la línea se quitó de `.gitignore`

En `news_sumary`: limpio (solo `data/.gitkeep`).

---

## 8. Qué cambia exactamente

- `TTS_API_BASE_URL` pasa a DNS de servicio `http://gateway:8001/api/v1` (Shape A) /
  nombre Tailscale (Shape B).
- `collections.toml` se mueve a `apps/news/collections.toml` → `deploy.sh` y el
  bind-mount se re-apuntan (**footgun conocido, commit `80a19a5`**, inodo `461207`).
- Las bases de datos se quedan separadas (`data/news.db` vs `storage/omnivoice.db`)
  según la decisión cerrada.
- `edge` sigue cliente-side; Modal intacto; kill switch `TTS_API_ENABLED` intacto.
- El Dockerfile de news-engine hoy hace `COPY data/ data/` (`data/` es gitignored) —
  arreglarlo al tocarlo.

---

## 9. Riesgos

- ~~**`nvidia-container-toolkit` necesita root en debwk**~~ — **obsoleto (2026-10-17):**
  ya está instalado (1.20.1) con spec CDI; `docker run --gpus all` funciona, sin root.
- **El gateway desplegado es la línea pre-rename** (`omnivoice_api` + `OMNIVOICE_*`,
  `dd874e3` + delta sin commitear). Containerizar desde `main` cambia la forma del
  código → requiere el port de P3 y una migración de variables.
- **`/opt/omnivoice` está VACÍO** en debwk → el bind-mount por defecto del compose
  montaría un directorio sin contenido; los engines reales viven en
  `~/ov_api/venv-omnivoice` y `~/qwen3-tts/venv`.
- **Basura en la historia del gateway** es permanente una vez importada → la purga
  debe aterrizar **antes** de `subtree add`.
- **Ruptura de tooling:** dos versiones de imagen Python (3.11 / 3.12), dos configs de
  ruff (news: `E,F,I,N,W,UP` vs gateway: ruleset completo), `fail_under=80` solo en
  news, `mypy --strict` con 133 errores en el gateway.
- **Los namespaces de Temporal viven en Postgres** — se recreen con
  `scripts/create_namespaces.sh` tras cualquier restore de Temporal.
- `deploy.sh`/`status.sh` asumen `~/news-summary`; los paths cambian.

---

## 10. Estado de la ejecución

**P0 — completado (2026-10-16):**

1. ✅ commit de higiene en `omni_api`: `2a8f10f` — 31 ficheros dejados de trackear
   (`MagicMock/**`, `tts_api.egg-info/**`, `mensaje2-8.txt`, `storage/`), artefactos
   borrados del disco, `.gitignore` corregido.
2. ✅ push `e5b7900..2a8f10f` y **PR #1 mergeado** en `choldi/ov_api/main` (`8a9d6f5`).
   CI ahora pasa `uv sync --dev`, `ruff check .` y `ruff format --check .`; se detiene
   en `mypy tts_api` (**P9**, aplazado a propósito).
3. ✅ `dd874e3` recuperado del servidor como ref **`deployed/main`** en el repo local;
   delta sin commitear de debwk capturado en
   `~/news-transfer/debkw-ov_api-uncommitted.patch` (+`pyproject.toml`, `uv.lock` +726,
   docs). El **port** del engine a `main` queda para **P3** (decisión: opción a).
4. ✅ backup en debwk `~/news-transfer/newsvoice-P0-backup-2026-10-16.tar.gz` (296M)
   con `news.db`, `manifest.json`, `collections.toml`, ambos `.env` y
   `storage/omnivoice.db`; 90 ficheros root-owned no legibles quedan en
   `…-skipped.log` (regenerables).

**P1 — completado (2026-10-17, repo `jordi/newsvoice`):**

1. ✅ repo creado en Gitea (`POST /api/v1/user/repos`, sin `auto_init`).
2. ✅ precondición: los 2 commits pendientes de news-engine empujados
   (`cc28af5..3e58fca`) para que el remoto lleve este plan.
3. ✅ `git subtree add --prefix=apps/news news main` (`d03accc`) y
   `--prefix=apps/gateway gateway main` (`003d584`) — **sin `--squash`**, ambas
   historias completas alcanzables (240 news + 149 gateway commits).
4. ✅ raíz uv workspace (`pyproject.toml`) + `libs/tts_contract` (esqueleto) — `f9fa52c`.
5. ✅ **criterio de aceptación ajustado** (decisión cerrada con el usuario): el
   `git log --follow apps/gateway/tts_api/main.py` de la §4 **no puede pasar** con
   `subtree add` (el graft pone el prefijo solo en el commit de merge; el histórico
   guarda rutas sin prefijo, `tts_api/main.py`). Se valida con
   `git log --follow 003d584^2 -- tts_api/main.py`, que sí alcanza la historia
   pre-rename. La historia 100% conservada; nada reescrito.

**P2 — compose completado (2026-10-17); deploy en debwk diferido a P10:**

1. ✅ `deploy/docker-compose.yml`: los 11 servicios de news + `gateway` (renombrado
   para no chocar con el `api` de news), `gateway` publicado como `${GATEWAY_PORT:-8001}:8000`,
   proyecto `newsvoice` fijado con `name:`. Validado con `docker compose config`.
2. ✅ `deploy/docker-compose.gpu.yml` (reserva nvidia) y `deploy/docker-compose.split.yml`
   (perfiles `news`/`gateway`). Verificado: `news`→10, `gateway`→1, ambos→11, ninguno→0.
3. ✅ `deploy/.env.example` — **solo** variables de compose (puertos, `TTS_PATH`,
   `COMPOSE_PROFILES`). **Desvío consciente de §2:** NO hay un `.env` único; cada app
   conserva el suyo (`apps/news/.env`, `apps/gateway/.env`) porque ambas definen
   `VOICES_DIR` con distinto significado/ruta y el gateway añade
   `CACHE_DIR`/`OUTPUTS_DIR`/`DATABASE_URL`.
4. ✅ GPU-in-container verificado en debwk vía CDI (`docker run --gpus all` expone
   `/dev/nvidia0`); el toolkit estaba ya instalado → **el prerrequisito root del plan
   era obsoleto**.
5. ⏸️ **Diferido a P10** (decisión del usuario): el cutover real en debwk (parar la
   pila `news-summary` de 11 contenedores + el `tts-api.service` de usuario, desplegar
   la pila unificada y verificar los curls). Bloqueantes conocidos: el engine del
   contenedor (imagen con extras vs venv externo) y el port del código desplegado
   (pre-rename) — ambos son P3/P10.

**P3 — completado (2026-10-17, rama `phase-3-engines`):**

1. ✅ `qwen3_tts` portado de `deployed/main` (`dd874e3`) a `apps/gateway/tts_api/`
   (rename `omnivoice_api`→`tts_api` resuelto): `core/engines/qwen3_tts_engine.py`
   (driver del worker), `qwen3_tts_worker.py` (protocolo de líneas ejecutado por el
   venv externo `QWEN3_TTS_PYTHON`, aislado por los pins de `transformers`) y
   `api/v1/engines.py` (catálogo con `locations`), router montado en `main.py`.
2. ✅ contrato de engine ampliado: `EngineCapabilities.locations` (`["local"]` por
   defecto, `edgetts` → `["cloud"]`) y `engine_name` opcional en los 3 `synthesize*`,
   propagado `engine_client` → `services/tts.py` → `api/v1/tts.py` (param `engine`)
   y honrado por `RoutedEngine._resolve_engine` (pinning por voz + tags separados
   por coma).
3. ✅ `settings.py`: `TTS_ENGINE=qwen3_tts`, bloque `QWEN3_TTS_*` y
   `qwen3_tts_python` (fallback `~/qwen3-tts/venv/bin/python`); `audio.py` 24000 Hz;
   `voice_service.CLONE_CAPABLE_ENGINES` con `qwen3_tts`; `omnilang_list` añade
   `ca`/`ru` cuando el routing lo encamina ahí.
4. ✅ tests nuevos `test_qwen3_tts_engine.py`, `test_engines_api.py` y
   `test_routed_engine.py` (unión de `locations` + override). Gate: **378 passed**,
   `ruff check` y `ruff format --check` limpios.
5. ⏸️ **`voxcpm2`/`zonos2` diferidos** (decisión del usuario): en debwk no hay
   runtime local (`zonos2`/`voxcpm` sin instalar en ningún venv) — hoy solo corren
   como imágenes de Modal y news-engine ya los invoca vía `is_modal()`. Un engine
   local exigiría venvs nuevos siguiendo el patrón worker de `qwen3_tts`, con lo
   que deja de ser un "port". Candidato de fase futura; el gateway conserva su
   stub `zono2`.

**P4 — completado (2026-10-17):**

1. ✅ `libs/tts_contract` **0.1.0** con cinco módulos: `errors`
   (`GatewayErrorDetail`/`GatewayErrorBody` + `parse_gateway_error`, que absorbe
   a la vez el `detail` anidado, el `detail` plano, el `error_type` de nivel
   superior del middleware 401 y el `error` del 429 de slowapi), `voices`
   (`StockVoice`, `ClonedVoice`, `CloneVoiceResponse`, `DesignedVoice`,
   `DesignedVoiceResponse`), `engines` (`EnginesResponse`), `system`
   (`HealthResponse`, `DiskUsage`) y `requests` (`TTSRequest`,
   `ConversationRequest`/`ConversationTurn`).
2. ✅ gateway: `tts-contract` añadido a `dependencies`; `response_model` declarado
   en `/disk`, `/engines`, `POST /voices/clone` y `POST /voices/design` (el
   cuerpo de éxito de `/tts` y `/conversations` es WAV, así que lo compartible de
   esos endpoints es su petición y su cuerpo de error — ya ambos cubiertos).
3. ✅ news-engine: `tts_backends.parse_gateway_error` y
   `narration_tts._error_from_response` delegan en el contrato (una sola
   implementación de desanidado); `voices`, `narration_tts` (health / stock /
   conversaciones) y `monitoring/checks` validan con los modelos; las peticiones
   a `/tts` y `/conversations` se construyen con `TTSRequest` y
   `ConversationRequest` en lugar de dicts literales.
4. ✅ tests nuevos: `apps/news/tests/unit/test_tts_contract.py` (13 casos, las
   cuatro formas de error + `DiskUsage` estricto) y
   `apps/gateway/tests/test_contract_models.py` (2 casos que parsean las
   respuestas reales). Gate: **1510 passed** (news) + **380 passed** (gateway),
   `ruff check` y `ruff format --check` limpios en las tres piezas.
5. 📌 Ejecutar tests locales con
   `PYTHONPATH=.:../../libs/tts_contract` (en build, el workspace `uv` resuelve
   `tts-contract` a partir de `[tool.uv.sources]` de la raíz).

**Siguiente: P5** — consolidar el dispatch de
`src/api/services/tts_backends.py` sobre `tts_contract`, conservando Edge por
defecto, el kill switch `TTS_API_ENABLED` y la regla `410 Gone`.
