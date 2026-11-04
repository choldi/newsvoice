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
2. **Elige el perfil** — Docker Shape A, desarrollo local, Docker Shape B o
   solo revisión; cada uno precarga sus valores por defecto sin pisar lo que
   ya hayas escrito a mano.
3. **Elige cómo instalar dependencias** — `uv sync` del workspace, un `.venv`
   por app con pip (`make venv`) o más tarde.
4. **Rellena los tres `.env`** — `deploy/.env`, `apps/news/.env` y
   `apps/gateway/.env` a partir de sus `.env.example`, sección por sección y
   con la explicación de cada variable. Los secretos se generan con `g`, la
   clave compartida del gateway se copia sola a `TTS_API_KEY` y `API_KEY`, y el
   hash bcrypt del administrador se crea desde su propio diálogo.
5. **Revisa el diff** antes de escribir nada; cada archivo se guarda con una
   copia `.bak-<fecha>`.
6. **Valida** — valores obligatorios, secretos coherentes entre archivos,
   plantillas sin rellenar, puertos, URL del gateway, dependencias y
   requisitos del host.

Teclas de la TUI: `↑/↓` mover · `Enter` editar/abrir · `Esc` atrás ·
`g` generar secreto · `a` opciones avanzadas · `Ctrl+R` valor por defecto ·
`?` ayuda · `q` salir.

La lógica vive en `scripts/setup_wizard/` (std-lib only) y está cubierta por
`apps/news/tests/unit/test_setup_wizard.py`.

## Estado

En construcción según `apps/news/MERGE_PLAN.md` (fases P0–P10). Remotos
originales (`jordi/news-summary`, `choldi/ov_api`) pasan a ser archivos de solo
lectura.
