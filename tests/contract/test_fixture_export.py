"""Versioned samples for the Rust control-plane client.

The files are deliberately generated from the Python contract definitions and
checked into the repository. Set ``FORGE_UPDATE_CONTRACT_FIXTURES=1`` when a
deliberate API change requires refreshing the reviewed payloads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from control_plane.api import app
from control_plane.schemas import RuntimeRunSnapshot

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts" / "fixtures"
OPENAPI = ROOT / "contracts" / "openapi" / "control-plane.json"


def _samples() -> dict[str, object]:
    """Representative wire payloads for each public operator representation.

    These use the exact field names and response wrappers defined by the
    FastAPI handlers/Pydantic schemas. IDs are stable only so git diffs expose
    contract changes clearly; they are not fixtures for database behavior.
    """
    run_id = "11111111-1111-1111-1111-111111111111"
    project_id = "22222222-2222-2222-2222-222222222222"
    now = "2026-07-12T00:00:00+00:00"
    return {
        "runtime-runs.json": [RuntimeRunSnapshot(
            id=run_id,
            project_id=project_id,
            project_name="fixture-project",
            goal_id="33333333-3333-3333-3333-333333333333",
            goal_title="Fixture goal",
            status="running",
            current_node="executor",
            batch=2,
            pid=4242,
            updated_at=now,
            active_task="Implement fixture",
            attempt_count=1,
            no_progress_count=0,
            task_total=3,
            task_completed=1,
            file_count=2,
            test_count=4,
            log_tail=["executor running"],
            model="fixture-model",
        ).model_dump(mode="json")],
        "durable-run.json": {
            "id": run_id,
            "project_id": project_id,
            "goal_id": "33333333-3333-3333-3333-333333333333",
            "provider_id": "executor",
            "status": "running",
            "current_node": "executor",
            "turn": 2,
            "max_turns": 24,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": now,
            "terminal_reason": None,
            "created_at": now,
            "updated_at": now,
        },
        "events.json": [{
            "id": "44444444-4444-4444-4444-444444444444",
            "run_id": run_id,
            "sequence": 1,
            "event_type": "run.running",
            "actor": "pge-supervisor",
            "payload": {"batch": 2},
            "created_at": now,
        }],
        "approvals.json": [{
            "id": "55555555-5555-5555-5555-555555555555",
            "run_id": run_id,
            "action_type": "external_action",
            "action_digest": "a" * 64,
            "action_preview": {"url": "https://example.test"},
            "risk": "high",
            "status": "pending",
            "requested_by": "planner",
            "decided_by": None,
            "decision_reason": None,
            "expires_at": "2026-07-12T00:15:00+00:00",
            "decided_at": None,
            "consumed_at": None,
            "created_at": now,
        }],
        "providers.json": {
            "version": 1,
            "profiles": {"executor": {
                "base_url": "http://127.0.0.1:1234/v1",
                "model": "fixture-model",
                "api_key": "sec…1234",
                "auth_mode": "api_key",
            }},
        },
        "run-start.json": {
            "status": "success", "started": True, "already_running": False,
            "run_id": run_id, "pid": 4242, "log": "/tmp/fixture.log",
            "source": "control-plane:web", "note": "Detached PGE supervisor started; lifecycle is durable in PostgreSQL.",
        },
        "run-stop.json": {"status": "stopped", "project_id": project_id, "pid": 4242},
        "errors.json": {
            "unauthorized": {"detail": "invalid control-plane credential"},
            "not_found": {"detail": "run not found"},
            "conflict": {"detail": "approval is no longer pending"},
        },
    }


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _assert_or_update(path: Path, value: object) -> None:
    rendered = _canonical(value)
    if os.getenv("FORGE_UPDATE_CONTRACT_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == rendered, f"contract fixture changed: {path.relative_to(ROOT)}"


def test_contract_fixtures_are_current() -> None:
    _assert_or_update(OPENAPI, app.openapi())
    for name, payload in _samples().items():
        _assert_or_update(FIXTURES / name, payload)


def test_runtime_fixture_keys_follow_the_handler_response_model() -> None:
    """Prevent a dashboard field from drifting away from its wire contract."""
    schema = app.openapi()["components"]["schemas"]["RuntimeRunSnapshot"]
    sample = _samples()["runtime-runs.json"][0]
    assert set(sample) == set(schema["properties"])
