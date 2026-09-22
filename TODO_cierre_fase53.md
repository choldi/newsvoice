# TODO - Cierre Fase 5.3 (System Monitor) — COMPLETADO

> Estado: Fase 5.3 **funcional y desplegada en prod** (deb-orch 192.168.5.7,
> repo `~/news-summary` @ `8582820`). Solo restan pasos administrativos para
> marcar los checkboxes de `Phases.md`. Verificado el 2026-09-21; pasado
> **2026-09-22** completo.

## Contexto verificado en prod
- Código actual + montaje docker.sock en worker (compose L43) presentes.
- `monitor_events` con 56 filas → migración `007_monitor.sql` aplicada y
  `SystemMonitorWorkflow` corriendo de forma continua.
- Último run de checks: docker_containers ok · tts_server_disk ok
  ("44.7% free (198G of 443G)") · edge_tts ok · services ok · digest_runs ok ·
  disk_local ok · collections WARN (feed medicalxpress.com timeout).
- Worker sin errores en 24h.

## Paso 1 — Marcar checkboxes y commitear (<= 5 min)
1. Repo dev local (`~/aider_projects/news_sumary`):
   - `git fetch`  (el `[ahead 12]` es stale: prod y Gitea ya están en 8582820)
   - `git status` → confirmar que no queda nada por pushear.
2. Editar `Phases.md` → Fase 5.3:
   - L193 `[ ] Deploy: montar socket docker...` → `[x]`
   - L194 `[ ] Endpoint GET /disk...` → `[x]` (nota: validado contra debwk,
     `tts_server_disk` ok 44.7% free)
3. `ruff check .` + `ruff format .` + `pytest -q` (expect ~348 passing)
4. Commit: `docs(phases): close PHASE 5.3 deploy` (via auto_commit.sh)
5. Push si hace falta.

## Paso 2 (opcional) — Default TTS base URL muerto ✅
- `src/core/config.py:88` y `scripts/status.sh:53` usaban
  `http://192.168.5.4:8000/api/v1` → **no responde**.
- Prod ya lo sobrescribe en `.env` con
  `http://debwk.elver-exponential.ts.net:8000/api/v1` (Tailscale).
- Hecho: cambiado el default en `config.py`, `status.sh` y las refs doc de
  `STATUS_CHECKS.md` (L28 y L198) a la URL de debwk.
- Commit: `fix(monitor): default tts_api_base_url apunta a debwk (Tailscale)`

## Paso 3 (opcional/aviso) — Feed con timeout ✅
- `collections` avisaba cada run: `medicalxpress.com/rss-feed/breaking/` → `400 Bad request`.
- Causa: el feed rechaza el UA por defecto de httpx (`python-httpx/*`); con
  `User-Agent: news-engine-monitor/1.0` responde 200 (no es un fallo de la red).
- Hecho: `_probe` de `checks.py` envía ahora un UA descriptivo
  (`_MONITOR_USER_AGENT`); añadido test
  `test_probe_sends_monitor_user_agent`. El pipeline no se bloqueaba.

## Verificación final
- `docker compose exec worker python -c "
  import asyncio; from src.api.services.monitoring.checks import run_all_checks
  async def m(): print(await run_all_checks())
  asyncio.run(m())"`
  → esperar `tts_server_disk ok` y `collections` sin "missing schedules".
- `make status` en prod.
- Alerta Telegram de prueba configurada (MONITOR_NOTIFY_CHANNELS=["telegram"]).

## Siguiente fase
- Fase 6 (opcional): Web UI + multi-user (criterio: Fases 1-5 estables).