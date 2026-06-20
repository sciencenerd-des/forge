
from fastapi.testclient import TestClient

from forge_runtime.tools import ToolContext, ToolRequest, default_registry


def test_model_command_execution_is_disabled_on_host_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_ALLOW_HOST_EXECUTION", "0")
    outside = tmp_path / "secret.txt"
    outside.write_text("host-secret")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = default_registry().execute(
        ToolContext(workspace=workspace, allow_shell=True),
        ToolRequest(
            "run_command",
            {"command": ["python3", "-c", f"print(open({str(outside)!r}).read())"]},
        ),
    )

    assert result.ok is False
    assert "host command execution is disabled" in (result.error or "")


def test_model_command_execution_can_be_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_ALLOW_HOST_EXECUTION", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = default_registry().execute(
        ToolContext(workspace=workspace, allow_shell=True),
        ToolRequest("run_command", {"command": ["python3", "-c", "print('ok')"]}),
    )

    assert result.ok is True
    assert result.data["stdout"] == "ok\n"


def test_runtime_routes_require_control_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}")
    monkeypatch.setenv("FORGE_CONTROL_TOKEN", "test-control-token")
    from control_plane.api import app

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for path in (
            "/runtime/runs",
            "/runtime/system",
            "/runtime/config",
            "/runtime/projects",
        ):
            assert client.get(path).status_code == 401

        response = client.get(
            "/runtime/system",
            headers={"Authorization": "Bearer test-control-token"},
        )
        assert response.status_code == 200
