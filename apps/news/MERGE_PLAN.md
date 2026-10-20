# MERGE_PLAN.md — Unificar `news-engine` y `tts-api` en un único repo

**Fecha:** 2026-10-16
**Estado:** plan aprobado; **P0** (2026-10-16), **P1** (2026-10-17), **P2**
(compose, 2026-10-17), **P3** (engines, 2026-10-17), **P4** (contrato, 2026-10-17),
**P5** (dispatch, 2026-10-17), **P6** (catálogo de voces, 2026-10-18), **P7**
(ops unificados, 2026-10-18), **P8** (lease de GPU, 2026-10-18) y **P9/P9.5**
(docs+CI / export-import, 2026-10-18) completados. **P10 (cutover) preparado en
debwk el 2026-10-19 (sin tocar producción); ejecución pendiente**.
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
en un único proyecto. El gateway se resuelve como `http://gateway:8000/api/v1`
(DNS de servicio → puerto interno 8000; el puerto 8001 solo se publica en el host).

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
| **P6** Catálogo de voces → gateway | ✅ `voice_profiles.json` movido a `apps/gateway/`; gateway dueño del catálogo (`VoiceCatalogService` + `GET/POST /api/v1/voices/catalog` y `.../refs/{name}`); news-engine lo lee con `voice_catalog_client` (snapshot + HTTP, sin filesystem local); uploads de referencias y voces custom pasan por el gateway | ✅ gates **1550 passed** (news full) + **403 passed** (gateway) verdes; `ruff check` + `ruff format --check` limpios en las tres piezas; `GET /admin/voices/catalog` sigue respondiendo (servido desde el snapshot del gateway) |
| **P7** Ops unificados | ✅ raíz con `Makefile` unificado (`help/venv/lint/format/format-check/test/test-news/test-gateway/coverage/check/commit/up/down/ps/health/logs-*/status/deploy/urls/clean`) y `scripts/` (`status.sh`, `deploy.sh`, `auto_commit.sh`); los Makefiles y scripts por app se conservan para desarrollo local; rutas apuntan al compose raíz (`deploy/docker-compose.yml`) y al catálogo del gateway | ✅ `make status` enumera los **12 servicios** + salud del gateway (y el resto de checks); `make lint` + `make format-check` limpios en las tres piezas; `make test` = **1550** (news) + **403** (gateway) verdes. Los scripts de export/import de P9.5 se integran cuando existan |
| **P8** Lease de GPU | ✅ la ventana de diseño entra en el lease de prioridad: `PRIORITY_DESIGN = 20` (por encima de digest 10 y render 0) + columna `note` en `tts_leases` (migración `014`) para saber *por qué* se tiene el motor; CLI nueva `python -m src.workers.design_lease {open,status,close}` que pide/renueva/libera el lease desde el host (Shape A, con cwd = raíz de la app; el worker de diseño sigue sin tocar la DB); `design_window.sh` lo invoca **antes** de parar el gateway (los renders en vuelo ceden en lugar de morir) y lo libera en el trap **después** de restaurarlo; escape hatch `DESIGN_WINDOW_NO_LEASE=1` para Shape B; `scripts/status.sh` con sección «3b. GPU engine lease» | ✅ **1570 passed** (news full, +20 sobre P7) + **403 passed** (gateway) verdes; `ruff check`/`format --check` limpios en las tres piezas; `bash -n` de `design_window.sh` y `status.sh`; docs: `Deploy.md` (orden de la ventana), `CONVENTIONS.md` (prioridades + `note`) |
| **P9** Docs + CI | Deploy.md §1-3 reescrito para Shapes A/B; `.github/workflows/ci.yml` (news/gateway/tts-contract, py3.11, ruff+mypy+pytest); `mypy tts_api` **172 → 0 errores** (strict) | CI verde en el monorepo (validado local) |
| **P9.5** Export/Import de config, voces, programación y boletines | `scripts/export_config.sh` → tarball `backups/newsvoice-config-<ts>.tar.gz` + `manifest.json` con checksums y conteos: **configuración** (`deploy/.env`, `apps/news/.env`, `apps/gateway/.env` —secretos documentados como sensibles— y `collections.toml`), **programación** (snapshot JSON de los schedules Temporal; la fuente de verdad sigue siendo `collections.toml` y al importar se re-registran con `python -m src.workers.schedule`), **voces** (catálogo runtime del gateway: `apps/gateway/storage/voice_catalog/` = `custom_profiles.json` + `manifest.json` + `refs/*.wav`, más checksum de `voice_profiles.json` commiteado) y **boletines/historial** (`data/news.db`: `deliveries`, `digest_runs`, `clusters`, …; el audio de `digest_runs.audio_path` solo con `--with-audio`). `scripts/import_config.sh`: `--dry-run` inventaría **lo que existe** vs lo que llega (crear/actualizar/conflicto/borrar); por defecto fusiona sin borrar lo local que no está en el tarball (lo sobrescrito guarda `.bak-<ts>`); **`--wipe` borra todo** lo gestionado antes de importar (voces runtime del gateway, schedules `digest-pipeline-*`, tablas de historial de `news.db`); `--with-env` para aplicar los `.env` (sin él solo informa diffs) | Export probado en local con tarball + checksums; `import_config.sh --dry-run` informa existentes/faltantes; `--wipe` verificado en local; schedules re-registrados y verificados contra el snapshot |
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

- `TTS_API_BASE_URL` pasa a DNS de servicio `http://gateway:8000/api/v1` (Shape A) /
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

---

## P6 — El catálogo de voces es del gateway (2026-10-18)

Decisión de alcance acordada con el usuario: el gateway es el dueño total del
catálogo — `voice_profiles.json`, `manifest.json`, `refs/*.wav` y
`custom_profiles.json` viven en `apps/gateway/` y los sirve por HTTP;
news-engine ya no tiene catálogo en disco.

1. ✅ `git mv apps/news/voice_profiles.json apps/gateway/voice_profiles.json`
   (32 perfiles; el committed catalog es la fuente de verdad).
2. ✅ **contrato** (`libs/tts_contract/tts_contract/catalog.py`): modelos
   `VoiceProfile`, `VoiceManifestEntry`, `VoiceCatalogResponse` y clasificador
   `classify_status` (`pendiente` / `generada` / `desactualizada`).
3. ✅ **gateway**: `VoiceCatalogService` (`apps/gateway/tts_api/services/`)
   con `catalog()`, `store_reference()`, `save_custom()`, `read_reference()`;
   router `apps/gateway/tts_api/api/v1/catalog.py`
   (`GET /api/v1/voices/catalog`, `GET|POST .../refs/{name}`,
   `POST .../custom`); errores `404/409/422/500` con `detail` plano.
4. ✅ **news-engine**: `src/api/services/voice_catalog_client.py` (cliente
   HTTP con snapshot, `fetch_catalog(max_age=...)`, `store_reference`,
   `save_custom_profile`, errores `VoiceCatalogRequestError.retryable`);
   `src/core/voice_catalog.py` re-exporta el contract; `voice_catalog_admin`,
   `voice_activities`, `gateway_voices`, `voice_preview`, `routes_ui` y
   `routes/admin_voices.py` consumen el cliente en vez del filesystem.
5. ✅ tests: news migra a `tests/fake_gateway.py` (`FakeGateway` con el
   catálogo commiteado real) — 1550 passed; gateway añade 8 tests de
   archivos corruptos/ilegibles — 403 passed.
6. 📌 `apps/news/data/voices/` ya no se usa; el export/import del catálogo
   queda para **P9.5**.

**Siguiente: P7** — ops unificados en la raíz (`Makefile` + `scripts/`,
`make status` con los 12 servicios + salud del gateway).

---

## P7 — Ops unificados en la raíz (2026-10-18)

La raíz del monorepo pasa a ser el entrypoint único de operaciones. Los
Makefiles y scripts de cada app se conservan tal cual para desarrollo local;
la raíz orquesta ambos.

1. ✅ `Makefile` raíz con targets unificados: `help`, `venv`/`install`,
   `lint`, `format`, `format-check`, `test`, `test-news`, `test-gateway`,
   `coverage`, `check`, `commit`, `up`, `down`, `ps`, `health`, `logs`,
   `logs-api`, `logs-gateway`, `logs-worker`, `logs-temporal`, `status`,
   `deploy`, `urls`, `clean`. El stack de docker sale de
   `deploy/docker-compose.yml` (los 12 servicios). Las rutas de
   catálogo/TTS apuntan al gateway (P6).
2. ✅ `scripts/status.sh`: health unificado — enumera los **12 servicios**,
   `/health` de news-engine, `/api/v1/health` del gateway (distingue
   `ok`/`degraded`), Temporal, workers, schedules, LLM, Telegram y SQLite.
3. ✅ `scripts/deploy.sh`: `up -d --build` del compose raíz + espera de
   salud de **ambos** (API news + gateway) + registro de schedules.
4. ✅ `scripts/auto_commit.sh`: ruff + pytest de news y gateway antes de
   commitear (equivalente raíz del de news).
5. 📌 `venv`/`check` asumen un `.venv` por app (o vars `*_PY`/`*_RUF`);
   en este checkout de validación se apuntó a los venvs externos de news
   y gateway. `make check` = `lint + format-check + test` en ambos.
6. 📌 Los scripts de export/import de config/voices (**P9.5**) se añadirán
   a este mismo `scripts/` cuando toque.

**Siguiente: P8** — lease de GPU: formalizar `design_window.sh` como lease
explícito para que síntesis y diseño no colisionen.

---

## P8 — Lease de GPU: la ventana de diseño entra en el lease (2026-10-18)

La ventana de diseño (síntesis de voces en la GPU) pasaba por parar el gateway
y descargar Ollama a mano, con dos riesgos: matar una síntesis en vuelo y que un
digest intentara sintetizar contra un gateway parado. Ahora la ventana es otro
participante del lease de prioridad de Fase 8.1.

1. ✅ **`PRIORITY_DESIGN = 20`** en `tts_lease.py` (por encima de digest 10 y
   render 0): al ponerse en cola, un render vivo *cede* antes de que el script
   pare el gateway, y los claims posteriores de digest esperan a la ventana.
2. ✅ **Migración `014_tts_lease_note.sql`**: `tts_leases.note` para registrar
   *por qué* se tiene el motor (`design-window` vs un `run_id` de digest).
   `acquire`/`hold` aceptan `note` (se escribe al reclamar y al takeover, el
   renew lo conserva); nuevos `holder_info()` y `waiters()` para introspección.
3. ✅ **CLI `python -m src.workers.design_lease {open,status,close}`**:
   `open` espera el turno (`wait_for_turn` con prioridad de diseño, por defecto
   900 s) y renueva hasta SIGINT/SIGTERM, liberando siempre en `finally`
   (incluye el caso de cancelación dentro de la limpieza de `wait_for_turn`);
   `status` imprime JSON (`enabled`/`holder`/`waiters`); `close` es idempotente.
   Se ejecuta desde el host con cwd = raíz de la app (**Shape A**): el worker de
   diseño no toca la base de datos — se mantiene el invariante de Fase 8.
4. ✅ **`design_window.sh`**: paso `0/5` pide el lease **antes** de tocar el
   gateway (y aborta sin tocar nada si no lo consigue); el `trap` lo libera
   **después** de restaurar el gateway; `DESIGN_WINDOW_NO_LEASE=1` es el escape
   hatch para Shape B (GPU box sin `data/news.db` compartida); `NEWS_PYTHON`
   permite apuntar al intérprete del venv.
5. ✅ **`scripts/status.sh`**: sección «3b. GPU engine lease» —
   `DESIGN WINDOW OPEN` destacado, motor libre, hold de digest/render o lease
   deshabilitado.
6. ✅ **Tests**: bloqueo cruzado diseño↔digest↔render, cesión del render ante
   un waiter de diseño, handoff sin solape, round-trip del `note` (claim,
   renew, takeover, `holder_info`), orden de `waiters()` y los flujos de la
   CLI (adquisición, cancelación en los tres puntos críticos, timeout, cierre
   idempotente).
7. 📌 Exclusiones conscientes (sin cambio de alcance): narración y clases siguen
   sin tomar el lease (decisión de Fase 8.x; con el gateway parado fallan con
   retries limpios); previews de GUI sin lease; sin endpoint de GUI para el
   lease (estado vía CLI y `make status`).

## P9 — Docs + CI: Deploy.md, CI workflow, mypy strict (2026-10-18)

1. ✅ **`mypy tts_api` strict — 172 → 0 errores**:
   - `apps/gateway/pyproject.toml`: `mypy_path = ["../../libs/tts_contract"]` + override `ignore_missing_imports` para motores opcionales (`torch`, `torchaudio`, `pocket_tts`, `qwen_tts`, `chatterbox`, `omnivoice`, `edge_tts`, `faster_whisper`, `piper`, `soundfile`, `pydub`).
   - 148 errores reales corregidos en 25 archivos (tipado genérico `dict[str, Any]`/`list[dict[str, Any]]`, anotaciones de retorno, `cast`/`Protocol` en vez de `Any`, async-generators → `AsyncIterator`).
   - Validado con mypy 2.3.1 (fijado en `apps/gateway/uv.lock`).

2. ✅ **CI monorepo** — `.github/workflows/ci.yml` (3 jobs paralelos):
   - `news-engine`: `uv sync --extra dev` → `ruff check/format` → `pytest` (1570 passed).
   - `tts-api (gateway)`: `uv sync --dev` → `ruff check/format` → `mypy --strict` → `pytest` (403 passed, `TTS_USE_MOCK=true`).
   - `tts-contract`: `uv sync` + `pip install ruff` → `ruff check/format`.
   - Python 3.11 único (matriz colapsada), cache de uv habilitado.

3. ✅ **`apps/news/Deploy.md` §1-3 reescrito para Shapes A/B**:
   - Prerequisitos, configuración (tres `.env` separados + `collections.toml`), arranque.
   - Shape A (todo en uno, overlay GPU opcional) y Shape B (app host + GPU host con perfiles `news`/`gateway`).
   - Referencia al root `Makefile` (`make up/status/health/logs/schedule/trigger/deploy`).

4. ✅ **Gates locales** — ruff limpio en las 3 piezas, 1570 + 403 tests, mypy 0, `bash -n` en scripts, YAML válido.

## P9.5 — Export/Import de config, voces, programación y boletines (2026-10-18)

1. ✅ **`scripts/export_config.sh`** — crea tarball `backups/newsvoice-config-<ts>.tar.gz` con:
   - Configuración: `deploy/.env`, `apps/news/.env`, `apps/gateway/.env`, `apps/news/collections.toml` (checksums SHA256 + tamaños en `manifest.json`).
   - Programación: snapshot JSON de schedules Temporal (`tctl schedule list --output json`).
   - Voces: catálogo runtime del gateway (`apps/gateway/storage/voice_catalog/` = `custom_profiles.json` + `manifest.json` + `refs/*.wav`) + checksum de `voice_profiles.json` commiteado.
   - Historial: `data/news.db` (tablas `deliveries`, `digest_runs`, `clusters`, `audio_jobs`, `narration_scripts`, `verifications`, `modal_spend`, `monitor_events`, `tts_leases`, `tts_lease_waiters`, `users`, `login_codes`, `sessions`, `kv_state`); audio (`audio_path`, `narration_path`) solo con `--with-audio`.

2. ✅ **`scripts/import_config.sh`** — restaura desde tarball con flags:
   - `--dry-run`: inventaria diffs (existente vs archivo) sin tocar nada.
   - `--with-env`: aplica `.env` y `collections.toml` (backup `.bak-<ts>` previo).
   - `--wipe`: borra recursos gestionados antes de importar (voice_catalog runtime, schedules `digest-pipeline-*`, tablas de historial en `news.db`).
   - Re-registra schedules Temporal desde `collections.toml` vía `python -m src.workers.schedule` (requiere worker arriba).

3. ✅ **Round-trip verificado**: export → modificación local → import `--with-env` restaura estado original; checksums validados.

4. ✅ **Gates** — `bash -n` limpio, ruff/format limpios, 1570 + 403 tests, mypy 0.

**Siguiente: P10** — cutover en debwk: desplegar monorepo, retirar checkouts `~/news-summary` y `~/ov_api`, archivar remotos.

---

## P10 — Cutover (COMPLETADO 2026-10-19)

**Estado:** ✅ cutover ejecutado y verificado en debwk. La pila `newsvoice`
(12 contenedores) reemplaza al stack legacy (`news-summary` + `tts-api.service`).
El digest end-to-end funciona: RSS → ollama → clustering → gateway TTS (GPU) →
Telegram (message_id 407, status ok). `tts-api.service` deshabilitada.

### Qué se preparó en este cambio

1. **Export legacy v2** — `scripts/export_config.sh` ahora detecta el layout
   (`--source monorepo|legacy`) y captura además el **runtime del gateway
   pre-merge**: `storage/omnivoice.db` (tablas `cloned_voices`,
   `designed_voices`), `melref.wav` y `storage/voices/<uuid>/reference.wav`.
   La P9.5 solo cubría el `voice_catalog/` moderno, que la caja legacy no tiene.
2. **Bug de colisión de `.env` corregido** — los tres `.env` se guardaban como
   `config/.env` (mismo basename) y solo sobrevivía uno; ahora son
   `config/deploy.env`, `config/apps-news.env`, `config/apps-gateway.env`.
   `import_config.sh` los mapea a sus destinos y restaura el gateway legacy.
3. **Engines en la imagen del gateway** — `apps/gateway/Dockerfile` compila con
   `--extra pocket_tts --extra edgetts --extra omnivoice` (ARG `UV_EXTRAS`). Los
   tres conmutables por config; **OmniVoice queda apagado por defecto**
   (`TTS_ENGINE=routed` → `pocket_tts`/`edgetts`), se activa solo si un job lo
   pide y hay GPU; sin GPU queda deshabilitado y ese backend va por Modal.
4. **Ops**: `scripts/create_namespaces.sh` (wrapper raíz) y targets
   `make namespaces|schedule|trigger`.
5. **`.gitignore`**: `backups/` ignorado; se destrackearon los tarballs de prueba
   que la P9.5 commiteó por error.
6. **Imágenes arregladas (bloqueante real de P10)** — las imágenes del gateway y
   de news nunca se habían ejercitado dentro de un contenedor (P8 difirió la
   containerización). Fallos encontrados y corregidos:
   - El gateway hacía `uv sync` (venv en `/app/.venv`) pero el `CMD`/healthcheck
     usaban el Python del sistema → `import fastapi` fallaba. Ahora
     `ENV VIRTUAL_ENV` + `PATH` activan el venv.
   - `tts-contract` es miembro del workspace y **no está publicado en PyPI**: el
     `pip install .` de news fallaba y el lock del gateway no lo incluía. Ambos
     Dockerfiles ahora compilan desde el **contexto raíz** con
     `uv sync --package <pkg>` contra el `uv.lock` de la raíz (que sí resuelve
     `tts-contract`); se añadió `.dockerignore` en la raíz.
   - `COPY voice_profiles.json` sobraba en news (P6 movió el catálogo al gateway).
   - Verificado en debwk: las 7 imágenes compilan y `import tts_contract`,
     `import src.api.main` (news) e `import tts_api.main` (gateway) funcionan.
7. **URL Shape A corregida** — `TTS_API_BASE_URL=http://gateway:8000/api/v1` (DNS
   de servicio → puerto **interno** del contenedor; el 8001 solo se publica en el
   host).

### Runbook de cutover (cuando toque)

Estado ya preparado en debwk: repo en `~/newsvoice` (rama `main`), config
importada (`.env` ×3, `collections.toml`), voces/DB del gateway y `news.db`
restaurados, `TTS_API_BASE_URL=http://gateway:8000/api/v1`, e imágenes ya
compiladas (`newsvoice-*`). El import solo es necesario si se rehace el staging.

```bash
# En debwk, repo ya preparado en ~/newsvoice
cd ~/newsvoice

# 0. Ventana: avisar. La app queda caída unos minutos.
# 1. Parar la pila vieja + el gateway systemd
( cd ~/news-summary && docker compose down )
systemctl --user stop tts-api.service
systemctl --user disable tts-api.service

# 2. (Si hace falta reimportar) config/voces/historial del export legacy
# ./scripts/import_config.sh backups/newsvoice-config-legacy-<ts>.tar.gz --with-env --wipe
#    y fijar apps/news/.env: TTS_API_BASE_URL=http://gateway:8000/api/v1

# 3. Levantar la pila unificada (debwk tiene GPU → overlay gpu).
#    Las imágenes ya están compiladas; --build opcional.
#    Alternativa de un solo comando (espera health + registra schedules):
#      GPU=1 MODAL=1 ./scripts/deploy.sh production
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml up -d

# 4. Namespaces + schedules (el volumen de Temporal del proyecto es nuevo)
make namespaces
make schedule

# 4b. Worker Modal (parity con el stack legacy, que lo corría)
docker compose -f deploy/docker-compose.yml --profile modal up -d narration-modal-worker

# 5. Verificar
curl -sf http://localhost:8000/health
curl -sf http://localhost:8001/api/v1/health
make trigger COLLECTION=tech        # digest de prueba
make status
```

### Log del cutover (debwk, 2026-10-19)

1. **Export legacy fresco** → `~/backups/newsvoice-config-legacy-20261019-142703.tar.gz`
   (5 schedules, 18 tablas, 34 voces).
2. **Baseline de datos** → `sudo rsync` de `~/news-summary/data/` a
   `apps/news/data/` (603M) + `chown -R 1001:65533` sobre `data/there`.
3. **Parar legacy** → `docker compose down` en `~/news-summary`; `tts-api.service`
   stop+disable; contenedor superviviente `news_engine_narration_modal_worker`
   eliminado; red `news-summary_default` eliminada.
4. **Import** → `import_config.sh … --with-env` (merge); adaptación
   `TTS_API_BASE_URL=http://gateway:8000/api/v1`; el gateway conserva
   `TTS_ENGINE=routed` + `TTS_ENGINES` + `API_KEY=` (paridad: abierto).
5. **Deploy** → `GPU=1 MODAL=1 ./scripts/deploy.sh production` → 12 contenedores,
   api/gateway healthy, 5 schedules registrados.
6. **Smoke test** → GPU real (`torch.cuda.is_available()==True`), síntesis TTS
   end-to-end vía DNS `gateway:8000`, `make trigger COLLECTION=tech` →
   Telegram `message_id 407, status ok`.

### Follow-ups (pendientes tras el cutover)

- **WhatsApp `there`**: sesión inválida tras el corte (Baileys) — requiere
  re-emparejamiento manual (QR). Canal secundario; el digest por Telegram funciona.
- **Narración gaming (OmniVoice)**: la colección `gaming` usa
  `tts_backend='omni_voice'` con voces diseñadas (UUIDs). El engine `routed`
  (pocket_tts/edgetts) no las conoce → «Voz no encontrada». El workflow garantiza
  que el digest de texto se entrega igual (narration failure ≠ digest failure).
  **Sidecar implementado** (`gateway-omnivoice`, imagen
  `apps/gateway/Dockerfile.omnivoice`, engine `omnivoice_remote` en el gateway):
  para activarlo, poner `OMNIVOICE_REMOTE_URL=http://gateway-omnivoice:8000`,
  añadir `omnivoice_remote` a `TTS_EXTRA_ENGINES` y retaggear (`UPDATE
  cloned_voices/designed_voices SET engine='omnivoice_remote'`) las voces que
  deban servirse por el sidecar. Alternativa sin sidecar: aceptar la voz stock o
  `TTS_FALLBACK_TO_EDGE=true` (pierde los personajes John/Peter).
- **Stock OmniVoice en español**: verificado 2026-10-20 contra el sidecar real:
  el clonado y el stock inglés funcionan, pero `es-mx-male` (y probablemente el
  resto de voces ES) mapea a un instruct `"male, spanish accent"` que el modelo
  no soporta (solo acepta los acentos listados) → `/tts` responde 503. Revisar
  el perfil de voces stock (`omnivoice_engine._get_mock_stock_voices`) o usar
  instructs sin acento para ES.
- **Purga legacy (1–2 semanas, con confirmación)**: retirar `~/news-summary`,
  `~/ov_api`, `~/qwen3-tts` y el volumen `news-summary_temporal_pg_data`. Ya hay
  tarballs finales en `~/backups/` (`news-summary-final`, `ov_api-final` con los
  modelos OmniVoice). No hacerlo hasta confirmar la estabilidad.
- **Rotar `SMTP_PASSWORD`** (el secreto viajó en el tarball de import).
- **Hardening API key**: el gateway quedó abierto (paridad). Considerar fijar
  `API_KEY` y rotar cuando se exponga fuera de la LAN/Tailscale.
- **Modo compose en `design_window.sh`**: el script maneja el gateway vía unit
  systemd (`TTS_GATEWAY_UNIT`) — necesita soporte compose.
- `VOICE_STAGING_API_KEY` y el worker de diseño se reconfiguran tras el corte.


