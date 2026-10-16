from __future__ import annotations

from sqlalchemy.exc import OperationalError

from control_plane import runtime_snapshot


def test_runtime_snapshots_preserve_manifest_evidence_when_engine_db_is_unavailable(
    monkeypatch,
) -> None:
    def unavailable_session():
        raise OperationalError("select 1", {}, OSError("database unavailable"))

    monkeypatch.setattr(runtime_snapshot, "SessionLocal", unavailable_session)
    monkeypatch.setattr(
        runtime_snapshot,
        "load_run_state",
        lambda: {
            "project-1": {
                "run_id": "run-1",
                "status": "running",
                "pid": 42,
                "batch": 3,
                "updated_at": "2026-07-13T20:00:00Z",
                "log": None,
                "invocation": {"goal": "Preserve durable evidence"},
            }
        },
    )
    monkeypatch.setattr(runtime_snapshot, "process_is_alive", lambda _pid: False)

    snapshots = runtime_snapshot.list_runtime_snapshots()

    assert len(snapshots) == 1
    assert snapshots[0]["id"] == "run-1"
    assert snapshots[0]["project_id"] == "project-1"
    assert snapshots[0]["goal_title"] == "Preserve durable evidence"
    assert snapshots[0]["status"] == "stopped"
    assert snapshots[0]["task_total"] == 0
