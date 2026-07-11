"""Read-only event-history projection and checkpoint divergence audit."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Any


def _value(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _event_key(event: Any) -> str:
    metadata = _value(event, "event_metadata", {}) or {}
    return str(metadata.get("idempotency_key") or _value(event, "id", ""))


def replay_state(db, project_id: str, goal_id: str | None = None) -> dict[str, Any]:
    """Fold durable events and materialized evidence into a fresh projection.

    The fold is pure with respect to the database: it performs reads only and
    deduplicates crash-replayed writes by their persisted idempotency key.
    """
    from app.models import HermesEvent, HermesFileChange, HermesTask, HermesTestRun

    task_query = db.query(HermesTask).filter(HermesTask.project_id == project_id)
    if goal_id:
        task_query = task_query.filter(HermesTask.goal_id == goal_id)
    tasks = task_query.order_by(HermesTask.created_at.asc(), HermesTask.id.asc()).all()
    task_ids = {task.id for task in tasks}

    events = db.query(HermesEvent).filter(HermesEvent.project_id == project_id).all()
    events.sort(key=lambda event: (
        _value(event, "created_at") or datetime.min,
        str(_value(event, "id", "")),
    ))

    projection: dict[str, Any] = {
        "project_id": project_id,
        "goal_id": goal_id,
        "task_statuses": OrderedDict((task.id, task.status) for task in tasks),
        "last_verdict": None,
        "files_touched": [],
        "tests_passed": [],
        "write_idempotency_keys": [],
        "event_ids": [],
    }
    seen_keys: set[str] = set()
    for event in events:
        if _value(event, "task_id") and _value(event, "task_id") not in task_ids:
            continue
        key = _event_key(event)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        projection["event_ids"].append(_value(event, "id"))
        metadata = _value(event, "event_metadata", {}) or {}
        event_type = str(_value(event, "event_type", ""))
        task_id = _value(event, "task_id")
        status = metadata.get("status")
        if event_type.startswith("task_") and task_id:
            projection["task_statuses"][task_id] = status or event_type.removeprefix("task_")
        if event_type in {"verdict", "evaluation", "task_verdict"}:
            projection["last_verdict"] = metadata.get("verdict") or _value(event, "content")
        if event_type == "file_write":
            path = metadata.get("path")
            if path and path not in projection["files_touched"]:
                projection["files_touched"].append(path)
            if metadata.get("idempotency_key"):
                projection["write_idempotency_keys"].append(metadata["idempotency_key"])
        if event_type in {"test_run", "verification"} and metadata.get("passed"):
            test_id = metadata.get("test_id") or _value(event, "content")
            if test_id not in projection["tests_passed"]:
                projection["tests_passed"].append(test_id)

    file_query = db.query(HermesFileChange).filter(HermesFileChange.project_id == project_id)
    test_query = db.query(HermesTestRun).filter(HermesTestRun.project_id == project_id)
    if task_ids:
        file_query = file_query.filter(HermesFileChange.task_id.in_(task_ids))
        test_query = test_query.filter(HermesTestRun.task_id.in_(task_ids))
    for change in file_query.order_by(HermesFileChange.created_at.asc()).all():
        if change.file_path not in projection["files_touched"]:
            projection["files_touched"].append(change.file_path)
    for test in test_query.order_by(HermesTestRun.created_at.asc()).all():
        if str(test.status).lower() in {"success", "passed", "pass"} and test.command not in projection["tests_passed"]:
            projection["tests_passed"].append(test.command)
    projection["task_statuses"] = dict(projection["task_statuses"])
    return projection


def diff_checkpoint(projection: dict[str, Any], checkpoint: Any) -> list[str]:
    """Return deterministic divergences without mutating the checkpoint."""
    state = _value(checkpoint, "current_state", {}) or {}
    if not isinstance(state, dict):
        return ["checkpoint current_state is not an object"]
    divergences: list[str] = []
    expected_tasks = state.get("task_statuses", {}) or {}
    actual_tasks = projection.get("task_statuses", {}) or {}
    for task_id in sorted(expected_tasks):
        if task_id not in actual_tasks:
            divergences.append(f"missing task in event history: {task_id}")
        elif actual_tasks[task_id] != expected_tasks[task_id]:
            divergences.append(
                f"contradictory task status {task_id}: checkpoint={expected_tasks[task_id]} history={actual_tasks[task_id]}"
            )
    expected_verdict = state.get("last_verdict")
    if expected_verdict is not None and projection.get("last_verdict") != expected_verdict:
        divergences.append(
            f"contradictory last verdict: checkpoint={expected_verdict} history={projection.get('last_verdict')}"
        )
    for path in state.get("files_touched", []) or []:
        if path not in projection.get("files_touched", []):
            divergences.append(f"checkpoint claims missing file change: {path}")
    for test_id in state.get("tests_passed", []) or []:
        if test_id not in projection.get("tests_passed", []):
            divergences.append(f"checkpoint claims unproven passing test: {test_id}")
    if state.get("goal_status") in {"completed", "complete", "verified"}:
        if not any(status == "completed" for status in actual_tasks.values()):
            divergences.append("phantom completion: checkpoint claims completed goal without completed task history")
    return divergences
