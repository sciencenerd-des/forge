"""Phase 5: event folding, idempotency, and checkpoint divergence detection."""
from datetime import datetime

from app import models as m
from forge_runtime.replay import diff_checkpoint, replay_state


def test_replay_deduplicates_write_events_and_detects_phantom_checkpoint(sqlite_db):
    project = m.HermesProject(name="p", repo_path="/tmp/p")
    sqlite_db.add(project)
    sqlite_db.commit()
    task = m.HermesTask(project_id=project.id, goal_id="goal", title="task", status="active")
    sqlite_db.add(task)
    sqlite_db.commit()
    first = m.HermesEvent(
        project_id=project.id, task_id=task.id, event_type="file_write", actor="executor",
        content="Wrote app.py", event_metadata={"path": "app.py", "idempotency_key": "k1"},
        created_at=datetime(2026, 1, 1))
    duplicate = m.HermesEvent(
        project_id=project.id, task_id=task.id, event_type="file_write", actor="executor",
        content="Wrote app.py again", event_metadata={"path": "app.py", "idempotency_key": "k1"},
        created_at=datetime(2026, 1, 2))
    sqlite_db.add_all([first, duplicate])
    sqlite_db.commit()
    projection = replay_state(sqlite_db, project.id)
    assert projection["files_touched"] == ["app.py"]
    assert projection["write_idempotency_keys"] == ["k1"]

    checkpoint = m.HermesCheckpoint(
        project_id=project.id, summary="bad", current_state={
            "goal_status": "completed", "task_statuses": {"missing": "completed"},
            "files_touched": ["not-in-history"],
        })
    divergences = diff_checkpoint(projection, checkpoint)
    assert any("missing task" in value for value in divergences)
    assert any("missing file" in value for value in divergences)
    assert any("phantom completion" in value for value in divergences)
