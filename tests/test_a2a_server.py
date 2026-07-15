from fastapi.testclient import TestClient


def test_agent_card_is_public_and_rpc_is_protected(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("FORGE_CONTROL_TOKEN", "test-token")
    from control_plane.api import app

    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {}}).status_code == 401


def test_agent_card_validates_against_official_sdk_types():
    from a2a.types import AgentCard
    from google.protobuf.json_format import ParseDict

    from forge_a2a.agent_card import agent_card

    parsed = ParseDict(agent_card(), AgentCard())

    assert parsed.name == "Forge"


def test_rpc_errors_are_json_rpc_shaped(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("FORGE_CONTROL_TOKEN", "test-token")
    from control_plane.api import app

    with TestClient(app) as client:
        response = client.post("/a2a", headers={"Authorization": "Bearer test-token"}, json={"jsonrpc": "2.0", "id": 1, "method": "unknown"})
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32601


def test_task_bridge_requires_existing_scoped_project(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    from forge_a2a import task_bridge

    class Db:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def get(self, *_args): return None

    monkeypatch.setattr(task_bridge, "SessionLocal", lambda: Db())
    try:
        task_bridge.create_task({"message": {"parts": [{"text": "do work"}]}, "metadata": {"project_id": "missing"}})
    except ValueError as exc:
        assert "not authorized" in str(exc)
    else:
        raise AssertionError("unscoped project was accepted")


def test_task_bridge_deduplicates_message_id(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    from forge_a2a import task_bridge

    class Project:
        repo_path = str(tmp_path / "workspace")

    class Goal:
        id = "goal-1"

    class Db:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def get(self, model, value): return Project() if value == "project-1" else None

    class Memory:
        def __init__(self, _db): pass
        def create_goal(self, **_kwargs): return Goal()

    monkeypatch.setattr(task_bridge, "SessionLocal", lambda: Db())
    monkeypatch.setattr(task_bridge, "MemoryService", Memory)
    monkeypatch.setattr(task_bridge, "_save", lambda value: task_bridge._path().write_text(__import__("json").dumps(value)))
    monkeypatch.setattr("pge_launcher.launch_pge", lambda *args, **kwargs: {"status": "success"})
    (tmp_path / "workspace").mkdir()
    params = {"message": {"messageId": "m-1", "parts": [{"text": "do work"}]}, "metadata": {"project_id": "project-1"}}

    first = task_bridge.create_task(params)
    second = task_bridge.create_task(params)

    assert first["id"] == second["id"]
