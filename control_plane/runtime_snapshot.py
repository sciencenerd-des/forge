from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import func

import forge_config
from app.database import SessionLocal
from app.models import HermesFileChange, HermesGoal, HermesProject, HermesTask, HermesTestRun
from pge_launcher import load_run_state, process_is_alive

GATEWAY_PROCESS_MARKER = "hermes_cli.main gateway run"


def get_config_snapshot() -> dict[str, Any]:
    """Resolved Forge configuration for the operator console — the real,
    env-driven provider + paths, so the GUI reflects exactly what a run will use.
    No secrets are returned (only the env var name backing the API key)."""
    prov = forge_config.provider_for("executor")
    db_url = forge_config.database_url()
    return {
        "home": str(forge_config.home()),
        "workspaces": str(forge_config.workspaces_root()),
        "database": db_url.split("@")[-1] if "@" in db_url else db_url,
        "default_project": forge_config.default_project_id() or "auto (resolve-or-create)",
        "provider": {
            "base_url": prov["base_url"],
            "model": prov["model"],
            "kind": _provider_kind(prov["base_url"]),
        },
        "roles": {
            role: forge_config.provider_for(role)["model"]
            for role in ("planner", "executor", "auditor", "evaluator", "steward")
        },
    }


def _provider_kind(base_url: str) -> str:
    u = base_url.lower()
    if ":1234" in u or "lmstudio" in u:
        return "LM Studio"
    if ":11434" in u or "ollama" in u:
        return "Ollama"
    if "openai.com" in u:
        return "OpenAI"
    if "anthropic" in u:
        return "Anthropic"
    if "localhost" in u or "127.0.0.1" in u:
        return "Local (OpenAI-compatible)"
    return "OpenAI-compatible"


def list_project_snapshots() -> list[dict[str, Any]]:
    return _list_project_snapshots()


def _list_project_snapshots() -> list[dict[str, Any]]:
    """Projects with their newest goal and task progress — for the Projects view."""
    out: list[dict[str, Any]] = []
    with SessionLocal() as db:
        for project in db.query(HermesProject).order_by(HermesProject.created_at.desc()).all():
            goals = (db.query(HermesGoal).filter(HermesGoal.project_id == project.id)
                     .order_by(HermesGoal.created_at.desc()).all())
            tasks = db.query(HermesTask).filter(HermesTask.project_id == project.id).all()
            completed = sum(1 for t in tasks if t.status == "completed")
            active_goal = next((g for g in goals if g.status == "active"), goals[0] if goals else None)
            out.append({
                "id": project.id,
                "name": project.name,
                "repo_path": project.repo_path,
                "goal_count": len(goals),
                "active_goal": active_goal.title if active_goal else None,
                "active_goal_status": active_goal.status if active_goal else None,
                "task_total": len(tasks),
                "task_completed": completed,
            })
    return out


def get_system_snapshot() -> dict[str, Any]:
    """Return read-only local process health for the operator console."""
    gateway_pid = _find_process(GATEWAY_PROCESS_MARKER)
    manifests = load_run_state()
    active_runs = sum(
        process_is_alive(manifest.get("pid"))
        for manifest in manifests.values()
    )
    return {
        "control_plane": {"status": "running", "pid": os.getpid()},
        "gateway": {
            "status": "running" if gateway_pid else "stopped",
            "pid": gateway_pid,
        },
        "active_pge_runs": active_runs,
    }


def list_runtime_snapshots() -> list[dict[str, Any]]:
    return _list_runtime_snapshots()


def _list_runtime_snapshots() -> list[dict[str, Any]]:
    manifests = load_run_state()
    snapshots: list[dict[str, Any]] = []
    with SessionLocal() as db:
        projects = {row.id: row for row in db.query(HermesProject).all()}
        for project_id, manifest in manifests.items():
            project = projects.get(project_id)
            goal = (db.query(HermesGoal).filter(HermesGoal.project_id == project_id)
                    .order_by(HermesGoal.created_at.desc()).first())
            tasks = (db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == goal.id if goal else False,
            ).order_by(HermesTask.created_at.asc()).all()) if goal else []
            task_ids = [task.id for task in tasks]
            file_count = test_count = 0
            if task_ids:
                file_count = db.query(func.count(HermesFileChange.id)).filter(
                    HermesFileChange.task_id.in_(task_ids)).scalar() or 0
                test_count = db.query(func.count(HermesTestRun.id)).filter(
                    HermesTestRun.task_id.in_(task_ids)).scalar() or 0
            active = next((task for task in tasks if task.status == "active"), None)
            completed = sum(task.status == "completed" for task in tasks)
            status = manifest.get("status", "unknown")
            if status in {"starting", "running", "launching"} and not process_is_alive(manifest.get("pid")):
                status = "stopped"
            log_tail = _tail(manifest.get("log"))
            snapshots.append({
                "id": manifest.get("run_id"),
                "project_id": project_id,
                "project_name": project.name if project else project_id,
                "goal_id": goal.id if goal else None,
                "goal_title": goal.title if goal else "No goal",
                "status": status,
                "current_node": _current_node(log_tail),
                "batch": manifest.get("batch", 0),
                "pid": manifest.get("pid"),
                "updated_at": manifest.get("updated_at"),
                "active_task": active.title if active else None,
                "attempt_count": active.attempt_count if active else 0,
                "no_progress_count": active.no_progress_count if active else 0,
                "task_total": len(tasks),
                "task_completed": completed,
                "file_count": file_count,
                "test_count": test_count,
                "log_tail": log_tail,
                "model": forge_config.provider_for("executor")["model"],
            })
    return sorted(snapshots, key=lambda item: item.get("updated_at") or "", reverse=True)


def _tail(path: str | None, lines: int = 12) -> list[str]:
    if not path:
        return []
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def _current_node(lines: list[str]) -> str:
    text = "\n".join(lines).lower()
    for node in ("evaluator", "executor", "auditor", "planner"):
        if node in text:
            return node
    return "waiting"


def _find_process(marker: str) -> int | None:
    try:
        result = subprocess.run(
            ["pgrep", "-f", marker],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for value in result.stdout.splitlines():
        try:
            pid = int(value)
        except ValueError:
            continue
        if pid != os.getpid() and process_is_alive(pid):
            return pid
    return None
