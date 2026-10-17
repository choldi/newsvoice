from fastapi.testclient import TestClient

from tts_api.main import app


def test_list_engines_returns_catalog() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/engines")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert isinstance(data["engines"], list)
    assert "active" in data
    # at least one engine reported
    names = {e["name"] for e in data["engines"]}
    assert "qwen3_tts" in names
    # locations present
    for e in data["engines"]:
        assert "locations" in e
