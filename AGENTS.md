# AGENTS.MD - Instrucciones para Antigravity

## CONTEXTO DEL PROYECTO

Estás trabajando en **news-engine**, un sistema modular self-hosted que genera un resumen diario de noticias curado por IA (formato: 5 historias principales + 5 titulares) a partir de feeds RSS, usando un LLM local (llama.cpp) y entregando el resultado vía Telegram/Email.

### Arquitectura: Hub and Spoke

- **Hub**: `Temporal.io` (orquestación durable, scheduling, retries, estado)
- **Spokes**: FastAPI microservicios + actividades Temporal (cada actividad = una responsabilidad)

## ARCHIVOS DE REFERENCIA OBLIGATORIOS

Antes de **CUALQUIER** tarea de codificación, DEBES leer y entender:

- `Architecture.md` — visión técnica, componentes, flujo de datos
- `CONVENTIONS.md` — reglas de estilo, naming, API, DB, Temporal, testing
- `Phases.md` — plan incremental de entrega (NO adelantes fases)
- `Deploy.md` — guia de despliegue
- `pyproject.toml` — dependencias permitidas

> **Regla**: Si hay conflicto entre este archivo y los anteriores, prevalecen los documentos de referencia.

## FASE ACTUAL

- **Fase activa**: Ver `Phases.md` y preguntar al usuario si no está claro.

### Reglas:
- NO implementes características de fases futuras
- Si el usuario pide algo fuera de fase, **ADVÍETE** antes de codificar
- Al completar una tarea, marca el checkbox correspondiente en `Phases.md`

## REGLAS DE CODIGO (Python)

### Stack obligatorio:
- Python 3.11+
- FastAPI + Pydantic v2
- LiteLLM (NUNCA OpenAI SDK directo)
- aiosqlite para DB
- structlog para logging
- pydantic-settings para configuración
- temporalio para workflows y actividades

### Estructura:
- Un endpoint por archivo en `src/api/routes/`
- Cada route expone un `APIRouter`
- Lógica de negocio en `src/api/services/`
- Modelos Pydantic en `src/api/models/` con sufijos `In/Out/DB`
- Prompts del LLM como constantes en `src/core/prompts.py`
- Workflows en `src/workers/workflows.py` y actividades en `src/workers/activities.py`

### Estilo:
- PEP 8
- Max 100 caracteres
- Type hints obligatorios en funciones públicas
- Docstrings Google style en funciones públicas
- Formatter: `ruff format`
- Linter: `ruff check`

### API:
- Todas las respuestas incluyen `{"status": "ok"|"error", ...}`
- Errores: `HTTPException` con detail descriptivo
- Endpoints stateless e idempotentes cuando sea posible
- TTS endpoint devuelve JSON con `audio_path`, NO binario

### Temporal (Workflows y Actividades):
- **Determinismo**: los workflows NO hacen I/O ni llamadas externas; todo efecto secundario vive en actividades
- Actividades llaman directo a `src/api/services/` (sin saltos HTTP)
- Toda llamada `execute_activity` lleva `start_to_close_timeout`
- Multi-argumento: usa `args=[...]` (NO posicionales extra)
- NO uses kwargs con el logger del sandbox (`workflow.logger.info("m", foo=1)` falla; usa f-strings)
- Tests de actividades: `ActivityEnvironment`
- Tests de workflows: `WorkflowEnvironment` con stubs registrados con el MISMO nombre que la actividad real

### Base de Datos:
- SQLite en `data/news.db`
- Migraciones manuales en `src/api/db/migrations/`
- NO uses Alembic en v1
- Tablas: `articles`, `clusters`, `deliveries`

### LLM:
- SIEMPRE vía wrapper en `src/core/llm.py`
- Temperature: **0.3** para resumenes, **0.7** para clustering
- NO llamas a OpenAI/llama.cpp SDK directamente desde routes ni workflows

### Configuración:
- `pydantic-settings` leyendo `.env`
- NO valores hardcoded de producción
- NO secrets en el código

### Testing:
- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` — tests `async def` sin marker)
- Unit tests para cada service (mock LLM, mock DB)
- Integration tests por endpoint con TestClient
- Tests de workflow con `WorkflowEnvironment`
- Coverage objetivo: **80%** en `src/api/services/`

## REGLAS DE GIT Y COMMITS

### Antes de cada commit:
1. Ejecuta estos comandos en orden:
   ```bash
   ruff check .
   ruff format .
   pytest -q
   ```

### IMPORTANTE:
- No hagas commit si los tests fallan o el linter reporta errores.

### Formato de commits (Conventional Commits):
- Usa este formato exacto: `<type>(<scope>): <descripción corta>`
- Tipos permitidos:
  - `feat` — nueva funcionalidad
  - `fix` — corrección de bug
  - `refactor` — cambio sin alterar comportamiento
  - `docs` — solo documentación
  - `test` — anadir o corregir tests
  - `chore` — configuración, CI, dependencias

### Ejemplos válidos:
- `feat(temporal): add worker service to docker-compose`
- `fix(dedup): handle URL normalization edge case`
- `docs(deploy): update Temporal schedule instructions`
- `test(workflow): add integration tests for DigestPipelineWorkflow`

### Reglas de commits:
- Un cambio lógico por commit.
- Si hiciste 3 cosas no relacionadas, haz 3 commits separados.
- Usa el script de auto-commit: `./scripts/auto_commit.sh "feat(scope): descripcion"`

### Estrategia de branches:
- `main` — estable, desplegable
- `phase-N-<nombre>` — branches de features (ej: `phase-3-cluster`)
- Merge a `main` via Pull Request cuando la fase esté completa
- **Nunca** hagas commit directo a main para Fase 2 en adelante

### Archivos que NUNCA debes commitear:
- **Secrets y configuración sensible**: `.env` (solo commitea `.env.example`), `.env.local`, `.env.*.local`, `*.pem`, `*.key`, `credentials.json`
- **Datos de runtime**: `data/` (todo el contenido, excepto `data/.gitkeep`), `*.db`, `*.db-journal`, `*.db-shm`, `*.db-wal`, `data/tts_cache/`
- **Artefactos de build**: `build/`, `dist/`, `*.egg-info/`, `.coverage`
- **Archivos de sistema e IDE**: `.vscode/`, `.idea/`, `.DS_Store`, `Thumbs.db`, `pycache/`, `*.pyc`
- **Binarios y modelos**: `models/`, `*.gguf`, `*.bin`

### Verificación antes de push:
1. Después de hacer commit, verifica que el push fue exitoso:
   ```bash
   git status
   git log --oneline -3
   ```
2. Si algo falla, **NO** intentes forzar el push. Revisa el error y pregunta al usuario.

## REGLAS DE DEPLOY

- No despliegues automáticamente sin confirmación explícita.
- Cuando te pidan deploy:
  ```bash
  ./scripts/deploy.sh production
  ```
- Confirma con el usuario antes de deploy a producción.
- Después de deploy, verifica (ver `Deploy.md`):
  ```bash
  curl http://localhost:8000/health
  docker compose logs -f worker
  ```
- Si falla, rollback inmediato (`docker compose down`).

## CHECKLIST DE FINALIZACIÓN DE TAREA

Antes de decir "hecho" a cualquier tarea, verifica:
- [ ] El código sigue `CONVENTIONS.md`
- [ ] Tests pasan: `pytest -q`
- [ ] Linter limpio: `ruff check .`
- [ ] Cambios commiteados con mensaje correcto
- [ ] Cambios pusheadados al remoto
- [ ] `Phases.md` actualizado si completaste un paso de fase
- [ ] No has introducido dependencias no listadas en `pyproject.toml`

## REGLAS ESPECÍFICAS DE ANTIGRAVITY

### Al iniciar sesión:
1. Lee todos los archivos de referencia (sección 2)
2. Pregunta al usuario en qué fase está trabajando
3. Muestra el checklist de esa fase antes de codificar

### Durante la codificación:
- Si no estás seguro de a qué fase pertenece una feature, **PREGUNTA**
- Si necesitas una dependencia nueva, **PREGUNTA** antes de anadirla
- Si un cambio requiere modificar la arquitectura, **ADVÍETE**

### Al completar una tarea:
1. Corre tests y linter
2. Si todo pasa, usa `./scripts/auto_commit.sh`
3. Actualiza `Phases.md` si corresponde
4. Resume lo que hiciste y lo que sigue

### Si algo falla:
- NO intentes "arreglarlo rápidamente" sin entender la causa
- Lee los logs: `docker compose logs api` / `docker compose logs worker`
  - Si es un error de tests, muestra el output completo
  - Si es un error de deploy, muestra el error del servidor

## EJEMPLOS DE INTERACCION CORRECTA

### ✅ Correcto:
- Usuario: "Implementa el workflow de digest diario"
- Mimo Code:
  1. Lee `Phases.md` y confirma que es Fase 3
  2. Muestra el checklist de Fase 3
  3. Implementa el workflow siguiendo `CONVENTIONS.md`
  4. Corre pytest y ruff
  5. Usa `./scripts/auto_commit.sh "feat(temporal): add DigestPipelineWorkflow"`
  6. Marca el checkbox en `Phases.md`

### ❌ Incorrecto:
- Usuario: "Anade collections y multi-usuario"
- Mimo Code:
  1. Lee `Phases.md` y ve que es Fase 5/6
  2. Si estamos en Fase 3, ADVÍETE: "Esto es Fase 5/6, quieres saltar fases?"
  3. Si el usuario confirma, implementa siguiendo las reglas
  4. **NO** implemente features de fases futuras sin confirmación

## COMANDOS UTILES

### Desarrollo:
```bash
docker compose up -d
pytest -q
ruff check .
ruff format .
```

### Commit y push:
```bash
./scripts/auto_commit.sh "feat(scope): descripcion"
```

### Deploy:
```bash
./scripts/deploy.sh production
```

### Logs:
```bash
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f temporal
```

### Health checks:
```bash
curl http://localhost:8000/health
```

### Temporal (dev):
```bash
# UI: http://localhost:8080
docker compose exec worker python -m src.workers.run_worker

# Registra un schedule POR COLECCIÓN (usa collections.toml)
docker compose exec worker python -m src.workers.schedule
docker compose exec worker python -m src.workers.schedule --collection tech

# Run manual de una colección
docker compose exec worker python -m src.workers.trigger --collection tech
```

## CONTACTO Y ESCALACIÓN

Si encuentras:
- Un conflicto entre documentos: pregunta al usuario
- Una feature ambigua: pregunta al usuario
- Un error que no puedes resolver: muestra logs y pregunta

**NUNCA asumas.** PREGUNTA.