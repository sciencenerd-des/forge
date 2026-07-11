from fastapi.testclient import TestClient


def test_provider_api_masks_and_preserves_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("FORGE_CONTROL_TOKEN", "test-token")
    from control_plane.api import app

    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        response = client.put("/providers/executor", json={
            "base_url": "http://localhost:1234/v1", "model": "model-a", "api_key": "secret-key-1234",
        }, headers=headers)
        assert response.status_code == 200
        assert response.json()["api_key"] != "secret-key-1234"

        response = client.put("/providers/executor", json={"model": "model-b", "api_key": ""}, headers=headers)
        assert response.status_code == 200
        assert response.json()["model"] == "model-b"

        listed = client.get("/providers", headers=headers)
        assert listed.status_code == 200
        assert "secret-key-1234" not in listed.text


def test_provider_api_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("FORGE_CONTROL_TOKEN", "test-token")
    from control_plane.api import app

    with TestClient(app) as client:
        assert client.get("/providers").status_code == 401
