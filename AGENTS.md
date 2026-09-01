# AGENTS.MD - Instrucciones para Antigravity

## CONTEXTO DEL PROYECTO

Estás trabajando en **news-engine**, un sistema modular self-hosted que genera un resumen diario de noticias curado por IA (formato: 5 historias principales + 5 titulares) a partir de feeds RSS, usando un LLM local (llama.cpp) y entregando el resultado vía Telegram/Email.

### Arquitectura: Hub and Spoke

- **Hub**: `n8n` (orquestación, scheduling, retries, notificaciones)
- **Spokes**: FastAPI microservicios (cada endpoint = una responsabilidad)

## ARCHIVOS DE REFERENCIA OBLIGATORIOS

Antes de **CUALQUIER** tarea de codificación, DEBES leer y entender:

- `Architecture.md` — visión técnica, componentes, flujo de datos
- `Conventions.md` — reglas de estilo, naming, API, DB, testing
- `Phases.md` — plan incremental de entrega (NO adelantes fases)
- `Deploy.md` — guia de despliegue
- `pyproject.toml` — dependencias permitidas

> **Regla**: Si hay conflicto entre este archivo y los anteriores, prevalecen los documentos de referencia.

## FASE ACTUAL

- **Fase activa**: Ver `Phases.md` y preguntar al usuario si no está claro.

### Reglas:
- NO implementes características de fases futuras
- NO anades clustering, TTS o Web UI antes de tiempo
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

### Estructura:
- Un endpoint por archivo en `src/api/routes/`
- Cada route expone un `APIRouter`
- Lógica de negocio en `src/api/services/`
- Modelos Pydantic en `src/api/models/` con sufijos `In/Out/DB`
- Prompts del LLM como constantes en `src/core/prompts.py`

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

### Base de Datos:
- SQLite en `data/news.db`
- Migraciones manuales en `src/api/db/migrations/`
- NO uses Alembic en v1
- Tablas: `articles`, `clusters`, `deliveries`

### LLM:
- SIEMPRE vía wrapper en `src/core/llm.py`
- Temperature: **0.3** para resumenes, **0.7** para clustering
- NO llamas a OpenAI/llama.cpp SDK directamente desde routes

### Configuración:
- `pydantic-settings` leyendo `.env`
- NO valores hardcoded de producción
- NO secrets en el código

### Testing:
- `pytest` + `pytest-asyncio`
- Unit tests para cada service (mock LLM, mock DB)
- Integration tests por endpoint con TestClient
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
- `feat(cluster): add fingerprint-based grouping`
- `fix(dedup): handle URL normalization edge case`
- `docs(deploy): update SSH instructions`
- `test(api): add integration tests for /filter_new`

### Reglas de commits:
- Un cambio lógico por commit.
- Si hiciste 3 cosas no relacionadas, haz 3 commits separados.
- Usa el script de auto-commit: `./scripts/auto_commit.sh "feat(scope): descripcion"`

### Estrategia de branches:
- `main` — estable, desplegable
- `phase-N-<nombre>` — branches de features (ej: `phase-2-dedup`)
- Merge a `main` via Pull Request cuando la fase esté completa
- **Nunca** hagas commit directo a main para Fase 2 en adelante

### Archivos que NUNCA debes commitear:
- **Secrets y configuración sensible**: `.env` (solo commitea `.env.example`), `.env.local`, `.env.*.local`, `*.pem`, `*.key`, `credentials.json`
- **Datos de runtime**: `data/` (todo el contenido, excepto `data/.gitkeep`), `*.db`, `*.db-journal`, `*.db-shm`, `*.db-wal`, `data/tts_cache/`
- **Archivos de sistema e IDE**: `.vscode/`, `.idea/`, `.DS_Store`, `Thumbs.db`, `pycache/`, `*.pyc`
- **Binarios y modelos**: `models/`, `*.gguf`, `*.bin`, `n8n_data/`

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
- Después de deploy, verifica:
  ```bash
  curl <server>/api/health
  ```
- Si falla, rollback inmediato (ver `Deploy.md`, sección 5).

## CHECKLIST DE FINALIZACIÓN DE TAREA

Antes de decir "hecho" a cualquier tarea, verifica:
- [ ] El código sigue `Conventions.md`
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
- Lee los logs: `docker compose logs api`
  - Si es un error de tests, muestra el output completo
  - Si es un error de deploy, muestra el error del servidor

## EJEMPLOS DE INTERACCION CORRECTA

### ✅ Correcto:
- Usuario: "Implementa el endpoint `/filter_new`"
- Mimo Code:
  1. Lee `Phases.md` y confirma que es Fase 2
  2. Muestra el checklist de Fase 2
  3. Implementa el endpoint siguiendo `Conventions.md`
  4. Corre pytest y ruff
  5. Usa `./scripts/auto_commit.sh "feat(dedup): add /filter_new endpoint"`
  6. Marca el checkbox en `Phases.md`

### ❌ Incorrecto:
- Usuario: "Anade clustering con embeddings"
- Mimo Code:
  1. Lee `Phases.md` y ve que es Fase 3
  2. Si estamos en Fase 1, ADVÍETE: "Esto es Fase 3, quieres saltar fases?"
  3. Si el usuario confirma, implementa siguiendo las reglas
  4. **NO** implemente clustering en Fase 1 sin confirmación

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
docker compose logs -f n8n
```

### Health checks:
```bash
curl http://localhost:8000/health
curl http://localhost:5678/healthz
```

## CONTACTO Y ESCALACIÓN

Si encuentras:
- Un conflicto entre documentos: pregunta al usuario
- Una feature ambigua: pregunta al usuario
- Un error que no puedes resolver: muestra logs y pregunta

**NUNCA asumas.** PREGUNTA.