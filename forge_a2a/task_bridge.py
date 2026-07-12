from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import forge_config
from app.database import SessionLocal
from app.models import ForgeGoal, ForgeProject
from app.services import MemoryService


def _path() -> Path:
    return forge_config.home() / "a2a_tasks.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "tasks": {}}


def _save(value: dict[str, Any]) -> None:
    path = _path()
    fd, temporary_name = tempfile.mkstemp(prefix=".a2a_tasks.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _message_text(message: dict[str, Any]) -> str:
    parts = message.get("parts", [])
    text = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")]
    return "\n".join(text).strip()


def create_task(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message") or {}
    text = _message_text(message)
    metadata = params.get("metadata") or message.get("metadata") or {}
    message_id = message.get("messageId") or metadata.get("message_id")
    project_id = metadata.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("A2A requests must include metadata.project_id")
    document = _load()
    if message_id:
        for existing in document["tasks"].values():
            if existing.get("message_id") == message_id and existing.get("context_id") == project_id:
                return task(existing["task_id"])
    task_id = str(uuid.uuid4())
    with SessionLocal() as db:
        project = db.get(ForgeProject, project_id)
        if project is None:
            raise ValueError("project_id is not authorized or does not exist")
        workspace = Path(project.repo_path or "").resolve()
        if str(workspace) in forge_config.forbidden_workspaces():
            raise ValueError("project workspace is forbidden")
        if not text:
            raise ValueError("message must contain text")
        goal = MemoryService(db).create_goal(project_id=project_id, title=text[:200], description=text)
        goal_id = goal.id
    document["tasks"][task_id] = {"task_id": task_id, "context_id": project_id, "goal_id": goal_id,
                                   "message_id": message_id, "status": "submitted"}
    _save(document)
    from pge_launcher import launch_pge
    launch = launch_pge(project_id, source="a2a", invocation={"goal": text, "task_id": task_id})
    if launch.get("status") != "success":
        document["tasks"][task_id]["status"] = "failed"
        _save(document)
        raise RuntimeError(launch.get("message", "run failed to start"))
    return task(task_id)


def task(task_id: str) -> dict[str, Any]:
    document = _load()
    record = document["tasks"].get(task_id)
    if record is None:
        raise KeyError(task_id)
    # Terminal states are final: a canceled task must not flip back to
    # "working" just because the underlying goal is still marked active.
    if record["status"] not in {"completed", "failed", "canceled"}:
        with SessionLocal() as db:
            goal = db.get(ForgeGoal, record["goal_id"])
            if goal is not None:
                if goal.status == "completed": record["status"] = "completed"
                elif goal.status in {"active", "proposed"}: record["status"] = "working"
                elif goal.status in {"blocked", "failed"}: record["status"] = "failed"
    if record["status"] in {"completed", "failed", "canceled"}:
        document["tasks"][task_id] = record
        _save(document)
    return {"id": task_id, "contextId": record["context_id"], "status": {"state": record["status"]}}


def cancel(task_id: str) -> dict[str, Any]:
    document = _load()
    record = document["tasks"].get(task_id)
    if record is None:
        raise KeyError(task_id)
    from pge_launcher import load_run_state, process_is_alive, update_run
    manifest = load_run_state().get(record["context_id"])
    if manifest and manifest.get("pid") and process_is_alive(manifest["pid"]):
        import signal
        os.kill(int(manifest["pid"]), signal.SIGTERM)
        update_run(record["context_id"], manifest.get("run_id"), status="stopped", terminal_reason="a2a_cancel")
    record["status"] = "canceled"
    document["tasks"][task_id] = record
    _save(document)
    return task(task_id)

