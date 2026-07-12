"""Versioned samples for the Rust control-plane client.

The files are deliberately generated from the Python contract definitions and
checked into the repository. Set ``FORGE_UPDATE_CONTRACT_FIXTURES=1`` when a
deliberate API change requires refreshing the reviewed payloads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.exceptions import ResponseValidationError

from .conftest import A2A_TASK_ID, APPROVAL_ID, HEADERS, PROJECT_ID, RUN_ID
from control_plane.api import app

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts" / "fixtures"
OPENAPI = ROOT / "contracts" / "openapi" / "control-plane.json"


def _json(response, expected_status: int):
    assert response.status_code == expected_status, response.text
    return response.json()


def _samples(client) -> dict[str, object]:
    """Capture wire payloads through real routes and response models."""
    _json(client.put("/providers/executor", headers=HEADERS, json={
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "fixture-model",
        "api_key": "secret-key-1234",
        "auth_mode": "api_key",
    }), 200)
    approvals = _json(client.get("/approvals", headers=HEADERS), 200)
    samples = {
        "runtime-runs.json": _json(client.get("/runtime/runs", headers=HEADERS), 200),
        "runtime-projects.json": _json(client.get("/runtime/projects", headers=HEADERS), 200),
        "durable-run.json": _json(client.get(f"/runs/{RUN_ID}", headers=HEADERS), 200),
        "events.json": _json(client.get(f"/runs/{RUN_ID}/events", headers=HEADERS), 200),
        "approvals.json": approvals,
        "providers.json": _json(client.get("/providers", headers=HEADERS), 200),
        "run-start.json": _json(client.post("/runtime/runs/start", headers=HEADERS, json={
            "goal": "Fixture goal", "description": "Implement fixture", "project_id": PROJECT_ID,
        }), 201),
        "run-stop.json": _json(client.post(f"/runtime/runs/{PROJECT_ID}/stop", headers=HEADERS), 200),
    }
    first_decision = _json(client.post(
        f"/approvals/{APPROVAL_ID}/decision",
        headers=HEADERS,
        json={"actor": "fixture", "approved": True, "reason": "Reviewed"},
    ), 200)
    assert first_decision["status"] == "approved"
    samples["errors.json"] = {
        "unauthorized": _json(client.get("/runtime/runs"), 401),
        "not_found": _json(client.get("/runs/missing", headers=HEADERS), 404),
        "conflict": _json(client.post(
            f"/approvals/{APPROVAL_ID}/decision",
            headers=HEADERS,
            json={"actor": "fixture", "approved": True, "reason": "Reviewed again"},
        ), 409),
    }

    def rpc(method: str, params: dict) -> dict:
        return _json(client.post("/a2a", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }), 200)

    samples["agent-card.json"] = _json(client.get("/.well-known/agent-card.json"), 200)
    send = rpc("message/send", {
        "metadata": {"project_id": PROJECT_ID},
        "message": {"parts": [{"text": "Fixture A2A goal"}]},
    })
    assert send["result"]["id"] == A2A_TASK_ID
    samples["a2a-send.json"] = send
    samples["a2a-get.json"] = rpc("tasks/get", {"id": A2A_TASK_ID})
    samples["a2a-cancel.json"] = rpc("tasks/cancel", {"id": A2A_TASK_ID})
    assert samples["a2a-cancel.json"]["result"]["status"]["state"] == "canceled"
    samples["a2a-errors.json"] = {
        "task_not_found": rpc("tasks/get", {"id": "missing"}),
        "missing_project": rpc("message/send", {"message": {"parts": [{"text": "x"}]}}),
        "unknown_method": rpc("tasks/unknown", {}),
    }
    return samples


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _assert_or_update(path: Path, value: object) -> None:
    rendered = _canonical(value)
    if os.getenv("FORGE_UPDATE_CONTRACT_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == rendered, f"contract fixture changed: {path.relative_to(ROOT)}"


def test_contract_fixtures_are_current(contract_client) -> None:
    _assert_or_update(OPENAPI, app.openapi())
    for name, payload in _samples(contract_client).items():
        _assert_or_update(FIXTURES / name, payload)


def test_runtime_fixture_keys_follow_the_handler_response_model(contract_client) -> None:
    """Prevent a dashboard field from drifting away from its wire contract."""
    schema = app.openapi()["components"]["schemas"]["RuntimeRunSnapshot"]
    sample = _samples(contract_client)["runtime-runs.json"][0]
    assert set(sample) == set(schema["properties"])


def test_runtime_handler_rejects_a_drifted_snapshot(contract_client, monkeypatch) -> None:
    """A dropped runtime field must fail at the HTTP boundary, not in Rust."""
    import control_plane.api as api

    monkeypatch.setattr(
        api,
        "list_runtime_snapshots",
        lambda: [{"id": RUN_ID, "project_id": PROJECT_ID}],
    )
    with pytest.raises(ResponseValidationError):
        contract_client.get("/runtime/runs", headers=HEADERS)
