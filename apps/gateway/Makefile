# Makefile para TTS API (multi-engine) - Compatible con Windows y Unix
# Uso: make <target> ENGINE=<engine>
# Engines: omnivoice (default), pocket_tts, edgetts, mock

.PHONY: help install install-base install-omnivoice install-pocket_tts install-edgetts install-all test lint format run dev clean check-gpu check-omnivoice-install download-model pre-commit test-unit test-integration test-load check-uv

# Engine selection (override with: make install ENGINE=pocket_tts)
ENGINE ?= omnivoice

# Detectar sistema operativo
ifeq ($(OS),Windows_NT)
    # Windows
    PYTHON := python
    VENV := .venv
    PIP := $(VENV)\Scripts\pip.exe
    PYTEST := $(VENV)\Scripts\pytest.exe
    RUFF := $(VENV)\Scripts\ruff.exe
    MYPY := $(VENV)\Scripts\mypy.exe
    UVICORN := $(VENV)\Scripts\uvicorn.exe
    PYTHON_VENV := $(VENV)\Scripts\python.exe
    ACTIVATE := $(VENV)\Scripts\activate.bat
    RM_RF := rmdir /s /q
    RM_F := del /f /q
    MKDIR_P := mkdir
    TOUCH := type nul >
    FIND_PYCACHE := for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
    FIND_PYC := del /s /q *.pyc
    NULL := nul
    SHELL := cmd.exe
    .SHELLFLAGS := /c
    TRUE_CMD := rem
else
    # Unix/Linux/macOS
    PYTHON := python3
    VENV := .venv
    PIP := $(VENV)/bin/pip
    PYTEST := $(VENV)/bin/pytest
    RUFF := $(VENV)/bin/ruff
    MYPY := $(VENV)/bin/mypy
    UVICORN := $(VENV)/bin/uvicorn
    PYTHON_VENV := $(VENV)/bin/python
    ACTIVATE := $(VENV)/bin/activate
    RM_RF := rm -rf
    RM_F := rm -f
    MKDIR_P := mkdir -p
    TOUCH := touch
    FIND_PYCACHE := find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    FIND_PYC := find . -type f -name "*.pyc" -delete
    NULL := /dev/null
    SHELL := /bin/bash
    TRUE_CMD := true
endif

# Default target
help:
	@echo "TTS API (multi-engine) - Comandos disponibles:"
	@echo ""
	@echo "  Engine selection: make install ENGINE=<engine>"
	@echo "    Engines: omnivoice (default), pocket_tts, edgetts, mock"
	@echo ""
	@echo "  make install              - Instala dependencias + engine $(ENGINE)"
	@echo "  make install-base         - Solo dependencias base (sin engine)"
	@echo "  make install-omnivoice    - Instala OmniVoice (GPU)"
	@echo "  make install-pocket_tts   - Instala Pocket TTS (CPU, voice cloning)"
	@echo "  make install-edgetts      - Instala EdgeTTS (cloud)"
	@echo "  make install-all          - Instala todos los engines"
	@echo "  make test                 - Ejecuta tests con cobertura"
	@echo "  make test-unit            - Ejecuta solo tests unitarios"
	@echo "  make test-integration     - Ejecuta tests de integración"
	@echo "  make test-load            - Ejecuta tests de carga"
	@echo "  make lint                 - Ejecuta ruff y mypy"
	@echo "  make format               - Formatea código con ruff"
	@echo "  make run                  - Inicia servidor en producción"
	@echo "  make dev                  - Inicia servidor en modo desarrollo (reload)"
	@echo "  make check-gpu            - Verifica disponibilidad de GPU/CUDA"
	@echo "  make check-omnivoice-install - Verifica la instalación externa de OmniVoice"
	@echo "  make download-model       - Descarga modelo OmniVoice desde HuggingFace"
	@echo "  make clean                - Limpia cachés y archivos temporales"
	@echo "  make pre-commit           - Instala y ejecuta pre-commit hooks"
	@echo ""

# --- Instalación ---

# Base dependencies only (no engine)
install-base: check-uv
	@echo "Creando entorno virtual en $(VENV)..."
	$(PYTHON) -m venv $(VENV)
	@echo "Actualizando pip..."
	$(PYTHON) -m pip install --upgrade pip
	@echo "Instalando dependencias del proyecto con uv..."
	uv sync --dev --project .
	@echo "Dependencias base instaladas en $(VENV)"

# Install base + selected engine
install: install-base install-$(ENGINE)
	@echo "Instalación completa: engine=$(ENGINE)"

# Engine-specific install targets
install-omnivoice:
	@echo "Instalando OmniVoice (GPU, torch+cu124)..."
	uv pip install "torch==2.5.1" "torchaudio==2.5.1" --python $(PYTHON_VENV) --index-url https://download.pytorch.org/whl/cu124
	uv pip install git+https://github.com/k2-fsa/OmniVoice.git --python $(PYTHON_VENV)
	@echo "Descargando modelo OmniVoice (ModelsLab/omnivoice-singing)..."
	$(PYTHON_VENV) scripts/download_model.py
	@echo "OmniVoice instalado"

install-pocket_tts:
	@echo "Instalando Pocket TTS (CPU, voice cloning)..."
	uv pip install "pocket-tts" --python $(PYTHON_VENV)
	@echo "Pocket TTS instalado"

install-edgetts:
	@echo "Instalando EdgeTTS (cloud)..."
	uv pip install "edge-tts" --python $(PYTHON_VENV)
	@echo "EdgeTTS instalado"

# Install all engines
install-all: install-base
	@echo "Instalando todos los engines..."
	uv pip install "pocket-tts" "edge-tts" --python $(PYTHON_VENV)
	uv pip install "torch==2.5.1" "torchaudio==2.5.1" --python $(PYTHON_VENV) --index-url https://download.pytorch.org/whl/cu124
	uv pip install git+https://github.com/k2-fsa/OmniVoice.git --python $(PYTHON_VENV)
	$(PYTHON_VENV) scripts/download_model.py
	@echo "Todos los engines instalados"

# Verifica que uv está disponible. Si no, intenta instalarlo.
ifeq ($(OS),Windows_NT)
check-uv:
	@where uv >$(NULL) 2>&1 || ( \
		echo [INFO] uv no encontrado. Intentando instalar con pip... && \
		$(PYTHON) -m pip install uv && \
		where uv >$(NULL) 2>&1 \
	) || ( \
		echo. && \
		echo ERROR: No se pudo instalar uv automaticamente. && \
		echo Instala uv manualmente: && \
		echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex" && \
		exit 1 \
	)
else
check-uv:
	@command -v uv >/dev/null 2>&1 || ( \
		echo "[INFO] uv no encontrado. Intentando instalar con pip..." && \
		$(PYTHON) -m pip install --user uv && \
		export PATH="$$HOME/.local/bin:$$PATH" && \
		command -v uv >/dev/null 2>&1 \
	) || ( \
		echo "" && \
		echo "ERROR: No se pudo instalar uv automaticamente." && \
		echo "Instala uv manualmente:" && \
		echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" && \
		exit 1 \
	)
endif

# Tests
test: install
	$(PYTEST) -v

test-unit: install
	$(PYTEST) -v -m "not integration"

test-integration: install
	$(PYTEST) -v -m "integration"

test-load: install
	$(PYTEST) tests/load/ -v --tb=short

# Calidad de código
lint: install
	$(RUFF) check .
	$(MYPY) tts_api

format: install
	$(RUFF) format .
	$(RUFF) check --fix .

# Servidor (engine se configura via TTS_ENGINE env var)
run: install
	TTS_ENGINE=$(ENGINE) $(UVICORN) tts_api.main:app --host 0.0.0.0 --port 8000

dev: install
	TTS_ENGINE=$(ENGINE) $(UVICORN) tts_api.main:app --host 0.0.0.0 --port 8000 --reload

# Utilidades
check-gpu: install
	@$(PYTHON_VENV) -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}'); print(f'CUDA version: {torch.version.cuda}'); print(f'GPU count: {torch.cuda.device_count()}'); [print(f'  GPU {i}: {torch.cuda.get_device_name(i)}') for i in range(torch.cuda.device_count())]"

check-omnivoice-install: install
	@echo "Verificando instalación externa de OmniVoice..."
	@$(PYTHON_VENV) scripts/check_omnivoice_install.py

download-model:
	@echo "Descargando modelo OmniVoice desde HuggingFace..."
	$(PYTHON_VENV) scripts/download_model.py

# Clean target with OS-specific commands
ifeq ($(OS),Windows_NT)
clean:
	$(RM_RF) $(VENV) 2>$(NULL) || $(TRUE_CMD)
	$(RM_RF) .mypy_cache 2>$(NULL) || $(TRUE_CMD)
	$(RM_RF) .ruff_cache 2>$(NULL) || $(TRUE_CMD)
	$(RM_RF) .pytest_cache 2>$(NULL) || $(TRUE_CMD)
	$(RM_RF) storage\outputs\* 2>$(NULL) || $(TRUE_CMD)
	$(FIND_PYCACHE)
	$(FIND_PYC)
	@echo "Cleanup complete"
else
clean:
	$(RM_RF) $(VENV) .mypy_cache .ruff_cache .pytest_cache 2>$(NULL) || $(TRUE_CMD)
	$(RM_RF) storage/outputs/* 2>$(NULL) || $(TRUE_CMD)
	$(FIND_PYCACHE)
	$(FIND_PYC)
	@echo "Cleanup complete"
endif

# Pre-commit
pre-commit: install
ifeq ($(OS),Windows_NT)
	$(VENV)\Scripts\pre-commit.exe install
	$(VENV)\Scripts\pre-commit.exe run --all-files
else
	$(VENV)/bin/pre-commit install
	$(VENV)/bin/pre-commit run --all-files
endif
