# MERGE_PLAN.md — Unificar `news-engine` y `tts-api` en un único repo

**Fecha:** 2026-10-16
**Estado:** plan aprobado; **P0 completado** (2026-10-16); **P1 completado**
(2026-10-17, repo `jordi/newsvoice`). Pendiente **P2**.
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
| 2 | **Container + nvidia toolkit** para el gateway | un solo entrypoint de deploy; instalar `nvidia-container-toolkit` en debwk es prerrequisito de P2 (root, una vez); publicado como `8001:8000` |
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

**Prerrequisito (root, una vez en debwk):** `nvidia-container-toolkit` +
`runtime: nvidia` — hoy ausente, así que el contenedor GPU de Shape A no puede
arrancar hasta instalarlo.

---

## 6. Fases (entregable + aceptación)

| Fase | Trabajo | Aceptación |
|---|---|---|
| **P0** Reconciliar y congelar | Purgar la basura (§7); diff del `dd874e3` desplegado (paquete `omnivoice_api`, engine real `qwen3_tts`) vs local; recuperar el commit del engine `qwen3_tts`; backup en debwk de `data/`, `storage/`, `.env`s | ✅ `git log` sin `MagicMock/` ni `*.egg-info`; tarball de backup en debwk; `dd874e3` **recuperado** en el repo del gateway como ref `deployed/main` (queda aquí, **no en `main`**). **Decisión:** portarlo a `main` (rename `omnivoice_api`→`tts_api` + conflictos) es **P3**, no P0 |
| **P1** Importar historias | subtree adds de arriba; raíz del workspace + docs de AGENTS/Phases fusionados | Ambas historias preservadas (149 gateway + 240 news commits alcanzables). `git log --follow 003d584^2 -- tts_api/main.py` alcanza la historia pre-rename (subtree: --follow desde HEAD no cruza el prefijo `apps/gateway/` por limitación de git; historia 100% conservada). |
| **P2** Un solo compose + GPU | `deploy/docker-compose.yml` (+ overlays gpu/split); servicio `gateway`; contrato de puerto 8001; instalar toolkit | Shape A verde en debwk: `curl /health` (news) + `curl :8001/api/v1/health` OK |
| **P3** Engines | Portar `qwen3_tts` (desde `dd874e3`), `voxcpm2`, `zonos2` respaldado por Modal a `apps/gateway`; **P3b Piper + P3c Chatterbox ya hechos** | cada engine accesible vía config routed; los gates de benchmark siguen siendo la autoridad |
| **P4** `libs/tts_contract` | modelos Pydantic compartidos de `/tts`, `/conversations`, `/voices/*`; ambas apps lo importan | news-engine deja de parsear JSON del gateway a mano |
| **P5** Consolidación de dispatch | reescribir `src/api/services/tts_backends.py` (549 líneas) sobre `tts_contract`; conservar Edge por defecto, kill switch `TTS_API_ENABLED` y la regla `410 Gone` | se mantiene el 80% de cobertura; digests byte-idénticos en `edge` |
| **P6** Catálogo de voces → gateway | `data/voices/manifest.json` + `voice_profiles.json` pasan a ser responsabilidad del gateway | `GET /admin/voices/catalog` servido desde el gateway |
| **P7** Ops unificados | un `Makefile`, `scripts/` fusionados, `make status` cubre ambas apps | `make status` reporta los 12 servicios + salud del gateway |
| **P8** Lease de GPU | formalizar `design_window.sh` como lease explícito para que síntesis y diseño no colisionen | test del lease + tabla `tts_leases` extendida |
| **P9** Docs + CI | Deploy.md reescrito para Shapes A/B; jobs de CI por app; arreglar `mypy tts_api` (133 errores) | CI verde en el monorepo |
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

- **`nvidia-container-toolkit` necesita root en debwk** — sin él, Shape A cae a A′
  (el gateway se queda en systemd). Decisión cerrada: instalarlo.
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

**Siguiente: P2** — un solo `deploy/docker-compose.yml` con servicio `gateway`,
contrato de puerto `8001:8000`, overlays gpu/split, y **prerrequisito root**:
instalar `nvidia-container-toolkit` en debwk.
