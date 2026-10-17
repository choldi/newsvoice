# Stress Tests con Locust

Tests de carga para OmniVoice API.

## Requisitos

```bash
pip install locust
```

## Ejecución

1. Inicia el servidor:
```bash
make run
# o
uvicorn tts_api.main:app --host 0.0.0.0 --port 8000
```

2. Ejecuta locust:
```bash
locust -f tests/load/locustfile.py --host=http://localhost:8000
```

3. Abre la UI en http://localhost:8089

4. Configura:
   - Number of users: 10-50
   - Spawn rate: 5 users/second

## Métricas objetivo

- 50 RPS sin OOM
- p95 latency < 3x single-request latency
- Sin fugas de archivos en storage/outputs/
