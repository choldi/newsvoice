# `aider_session.sh`

Wrapper robusto para lanzar sesiones de [Aider](https://aider.chat/) con:

- Selección interactiva de modelos desde un YAML.
- Entorno (endpoints y API keys) específico por modelo.
- Selección segura de modelos `weak` y `editor`.
- Logging detallado por sesión.
- Rotación y control de tamaño de la historia de Aider.
- Limpieza automática de sesiones antiguas.
- Resumen y diagnóstico automático al finalizar la sesión.

---

## Características

- **Selección de modelo** desde `.aider_model_selection.yml` por número o ID, con iconos y grupos.
- **Modo no interactivo**: si la entrada no es una TTY, usa el modelo por defecto (o `AIDER_MODEL`).
- **Entorno por modelo**: exporta las variables `env:` del modelo elegido, expandiendo `${VAR}` desde el entorno/`.env`, y limpia variables de otros proveedores para evitar llamar a un servidor equivocado.
- **Weak/editor compatibles**: solo añade `--weak-model` / `--editor-model` si pertenecen al mismo `group` (o proveedor/endpoint) que el modelo principal.
- **Logging con niveles** (`INFO`, `OK`, `WARN`, `ERROR`, `DEBUG`) a consola y a fichero de sesión.
- **Rotación de historia** por tamaño, con número de versiones configurable y tamaño total máximo.
- **Resumen post-sesión** en Markdown con duración, modelos, argumentos, entorno (claves ocultas), rotaciones, diagnóstico y estado de Git.
- **Diagnóstico automático**: detecta errores de autenticación (401/403), endpoints inexistentes (404), problemas de red, YAML inválido, etc.
- **Captura opcional** de la salida completa de aider (`AIDER_CAPTURE_OUTPUT=1`) para mejorar el diagnóstico.
- **Portátil**: funciona en GNU/Linux y macOS (con fallbacks para `stat`, `script`, etc.).

---

## Requisitos

| Requisito   | Obligatorio | Notas                                                        |
|-------------|:-----------:|--------------------------------------------------------------|
| `bash` ≥ 4  | ✅          |                                                              |
| `aider`     | ✅          | Debe estar en el `PATH`.                                     |
| `python3`   | ✅          | Para leer el YAML de modelos.                                |
| `PyYAML`    | ⚠️          | `pip install pyyaml`. Si falta, el script degrada al modelo por defecto. |
| `git`       | ❌          | Solo para enriquecer el resumen.                             |
| `timeout`   | ❌          | Para limitar la duración de `scripts/pre_prompt.sh`.         |
| `script`    | ❌          | Solo si usas `AIDER_CAPTURE_OUTPUT=1`.                       |

---

## Instalación

```bash
chmod +x scripts/aider_session.sh
```

El script detecta automáticamente la raíz del proyecto como el directorio padre de `scripts/` y trabaja desde ahí.

---

## Uso básico

```bash
./scripts/aider_session.sh                      # menú interactivo
./scripts/aider_session.sh --map-mode code      # argumentos extra para aider
AIDER_MODEL=openai/free-stack-1 ./scripts/aider_session.sh   # sin menú
```

---

## Flujo de ejecución

1. Carga `.env` si existe.
2. Ejecuta `scripts/pre_prompt.sh` (con timeout) para generar contexto fresco.
3. Rota los ficheros de historia si superan el tamaño máximo (fase *pre-session*).
4. Limpia sesiones antiguas (por número y tamaño total).
5. Valida `.aider_model_selection.yml`.
6. Selecciona el modelo (interactivo, `AIDER_MODEL` o por defecto).
7. Exporta el entorno del modelo y limpia variables de otros proveedores.
8. Elige `--weak-model` y `--editor-model` compatibles (según configuración).
9. Añade `--read .aider_fresh_context.md` si existe, más argumentos extra.
10. Lanza `aider` (opcionalmente capturando la salida).
11. Al salir (también en caso de error): rota la historia (*after-session*), limpia sesiones y genera el **resumen + diagnóstico** en Markdown.

---

## Configuración

Todas las opciones se configuran con variables de entorno:

| Variable | Defecto | Descripción |
|----------|---------|-------------|
| `AIDER_MODEL_FILE` | `.aider_model_selection.yml` | YAML con la lista de modelos. |
| `AIDER_DEFAULT_MODEL` | `openai/local-qwen` | Modelo por defecto (selección inválida o modo no interactivo). |
| `AIDER_MODEL` | *(vacío)* | Fuerza un modelo por número o ID sin mostrar el menú. |
| `AIDER_SESSION_LOG_DIR` | `logs/aider_sessions` | Directorio de logs, resúmenes y transcripts. |
| `AIDER_HISTORY_FILES` | `.aider.chat.history.md .aider.input.history` | Ficheros de historia a rotar (separados por espacios). |
| `AIDER_HISTORY_MAX_SIZE` | `5M` | Tamaño máximo por fichero de historia antes de rotar (`K`, `M`, `G` o bytes). |
| `AIDER_HISTORY_KEEP` | `5` | Versiones rotadas a mantener por fichero (`file.1` … `file.N`). `0` trunca en vez de rotar. |
| `AIDER_HISTORY_MAX_TOTAL_SIZE` | `50M` | Tamaño máximo del conjunto *fichero + rotados*; borra los rotados más antiguos. |
| `AIDER_HISTORY_ROTATE_AFTER` | `1` | Rota la historia también al terminar la sesión. |
| `AIDER_CLEAN_PROVIDER_ENV` | `1` | Hace `unset` de variables conocidas de otros proveedores antes de aplicar las del modelo elegido. |
| `AIDER_ENABLE_WEAK_MODEL` | `auto` | `auto`/`1`: solo si es compatible · `0`: desactivado · `force`: siempre. |
| `AIDER_ENABLE_EDITOR_MODEL` | `auto` | Igual que el anterior, para `--editor-model`. |
| `AIDER_ALLOW_CROSS_AUX_MODELS` | `0` | `1` permite weak/editor de otro proveedor/endpoint (no recomendado). |
| `AIDER_CAPTURE_OUTPUT` | `0` | `1` captura toda la salida de aider con `script` en un transcript. |
| `AIDER_PRE_PROMPT_TIMEOUT` | `120` | Segundos máximos para `scripts/pre_prompt.sh`. |
| `AIDER_SESSION_KEEP` | `30` | Número de sesiones (logs/resúmenes) a conservar. |
| `AIDER_SESSION_MAX_TOTAL_SIZE` | `200M` | Tamaño máximo total de todos los ficheros de sesiones. |
| `AIDER_EXTRA_ARGS` | *(vacío)* | Argumentos extra para aider (separados por espacios). |
| `AIDER_DEBUG` | `0` | `1` activa logs de nivel `DEBUG`. |

### Variables de `.env` usadas por el YAML

| Variable | Uso |
|----------|-----|
| `OPENROUTER_API_KEY` | Modelos `openrouter/...` |
| `NVIDIA_NIM_API_KEY` | Modelos `nvidia_nim/...` |
| `OMNIROUTE_API_KEY` | Modelos Omniroute (`openai/free-stack-1`) |

---

## Formato del YAML de modelos

El fichero debe ser YAML válido con una lista `models:`. Campos:

| Campo | Obligatorio | Descripción |
|-------|:-----------:|-------------|
| `id` | ✅ | Identificador de modelo para Aider (p. ej. `openai/local-phi`). |
| `name` | ✅ | Nombre mostrado en el menú. |
| `type` | ✅ | `main`, `weak` o `editor`. |
| `icon` | ❌ | Emoji mostrado en el menú. |
| `group` | ❌ | Grupo/proveedor; usado para elegir weak/editor compatibles. |
| `auth_required` | ❌ | Informativo. |
| `env` | ❌ | Mapa de variables de entorno; los `${VAR}` se expanden desde el entorno. |

Ejemplo:

```yaml
models:
  - id: "openai/local-phi"
    name: "🖥️ local-phi (ollama local)"
    icon: "🖥️"
    group: "local"
    type: "main"
    auth_required: false
    env:
      OPENAI_API_BASE: "http://debwk.elver-exponential.ts.net:6000/v1"

  - id: "openai/free-stack-1"
    name: "🌐 Free stack, varios modelos"
    icon: "🌐"
    group: "omniroute"
    type: "main"
    env:
      OPENAI_API_BASE: "http://localhost:20128/v1"
      OPENAI_API_KEY: "${OMNIROUTE_API_KEY}"
```

Validación rápida:

```bash
python3 -c "import yaml; yaml.safe_load(open('.aider_model_selection.yml')); print('YAML OK')"
```

---

## Rotación de la historia

Si `.aider.chat.history.md` supera `AIDER_HISTORY_MAX_SIZE`:

```text
.aider.chat.history.md.4  →  .aider.chat.history.md.5
.aider.chat.history.md.3  →  .aider.chat.history.md.4
.aider.chat.history.md.2  →  .aider.chat.history.md.3
.aider.chat.history.md.1  →  .aider.chat.history.md.2
.aider.chat.history.md    →  .aider.chat.history.md.1   (y se crea uno nuevo vacío)
```

- Se conservan como máximo `AIDER_HISTORY_KEEP` versiones.
- Si el conjunto (actual + rotados) supera `AIDER_HISTORY_MAX_TOTAL_SIZE`, se eliminan los rotados más antiguos.
- La rotación se ejecuta **antes** y **después** de cada sesión.
- Cada rotación queda anotada en el log y en el resumen.

---

## Ficheros generados por sesión

```text
logs/aider_sessions/
├── aider_session_20260818_101530_12345.log           # log completo del script
├── aider_session_20260818_101530_12345.summary.md    # resumen + diagnóstico
└── aider_session_20260818_101530_12345.transcript.log  # solo con AIDER_CAPTURE_OUTPUT=1
```

Las sesiones antiguas se limpian automáticamente según `AIDER_SESSION_KEEP` y `AIDER_SESSION_MAX_TOTAL_SIZE` (la sesión en curso nunca se borra).

### Contenido del resumen

- Fecha, duración y código de salida.
- Modelo principal, weak y editor.
- Argumentos finales pasados a `aider`.
- Variables de entorno de proveedor (**claves ocultas** como `***`).
- Rotaciones de historia realizadas.
- **Diagnóstico automático** (ver abajo).
- Estado de Git: branch, HEAD antes/después, diff stat, ficheros cambiados, últimos commits.
- Últimas 80 líneas del log (y del transcript, si existe).

### Diagnóstico automático

El resumen (y la consola al terminar) avisa de:

- Código de salida distinto de 0.
- Python/PyYAML ausente o YAML inválido.
- Errores/avisos contados en el log.
- Posibles fallos de autenticación: `401`, `403`, `unauthorized`, `invalid api key`.
- Posibles endpoints/modelos inexistentes: `404`, `not found`.
- Posibles problemas de red: `connection refused`, `timeout`, `could not resolve`.
- Uso de weak/editor potencialmente incompatibles con el modelo principal.

---

## Ejemplos

```bash
# Sesión normal con menú
./scripts/aider_session.sh

# Forzar el último modelo del YAML
AIDER_MODEL=openai/free-stack-1 ./scripts/aider_session.sh

# Sin weak ni editor (evita mezclar servidores)
AIDER_ENABLE_WEAK_MODEL=0 AIDER_ENABLE_EDITOR_MODEL=0 ./scripts/aider_session.sh

# Máximo detalle para investigar un problema
AIDER_DEBUG=1 AIDER_CAPTURE_OUTPUT=1 ./scripts/aider_session.sh

# Rotación más agresiva de la historia
AIDER_HISTORY_MAX_SIZE=2M AIDER_HISTORY_KEEP=3 AIDER_HISTORY_MAX_TOTAL_SIZE=20M \
  ./scripts/aider_session.sh

# Argumentos extra para aider
./scripts/aider_session.sh --map-mode code --yes
AIDER_EXTRA_ARGS="--map-mode code" ./scripts/aider_session.sh
```

---

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---------|----------------|----------|
| «Me envía a un servidor erróneo» | Weak/editor de otro proveedor, o variables antiguas de `.env` | Revisa el `OPENAI_API_BASE` en el log/resumen; usa `group` en el YAML, `AIDER_ENABLE_WEAK_MODEL=0`, o mantén `AIDER_CLEAN_PROVIDER_ENV=1`. |
| `YAML inválido` al arrancar | YAML mal formado (textos sueltos, sin guiones de lista) | Corrige el YAML (ver formato arriba) y valida con `python3 -c "import yaml; ..."`. |
| «No se encontró PyYAML» | Falta el módulo | `pip install pyyaml` (el script sigue funcionando con el modelo por defecto). |
| La historia crece sin límite | Rotación desactivada o límites muy altos | Ajusta `AIDER_HISTORY_MAX_SIZE`, `AIDER_HISTORY_KEEP` y `AIDER_HISTORY_MAX_TOTAL_SIZE`. |
| El resumen no detecta errores de aider | Salida no capturada | Usa `AIDER_CAPTURE_OUTPUT=1`. |
| `pre_prompt.sh` cuelga la sesión | Script lento o bloqueado | Reduce `AIDER_PRE_PROMPT_TIMEOUT` o instala `timeout`. |

---

## Códigos de salida

El script termina con el **mismo código de salida que `aider`**. Cualquier error interno del wrapper queda registrado en el log y en el resumen (trap de `ERR`/`EXIT`).

---

## Notas

- Los iconos y el campo `group` del YAML son opcionales pero recomendados: los iconos mejoran el menú y `group` evita combinar modelos de proveedores distintos.
- El script nunca muestra el valor de las API keys: en logs y resumen aparecen como `***`.

