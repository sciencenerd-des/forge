"""Canonical detached launcher and lifecycle manifest for PGE runs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import forge_config

ROOT = Path(__file__).resolve().parent
RUN_DIR = forge_config.home() / "logs" / "pge_runs"
RUN_STATE = RUN_DIR / "runs.json"
# Use the interpreter Forge is running under (the project's venv), overridable.
PYTHON = Path(os.getenv("FORGE_PYTHON", sys.executable))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def load_run_state() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(RUN_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_run_state(state: dict[str, dict[str, Any]]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUN_STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, RUN_STATE)


def _persist_lifecycle(record: dict[str, Any], event_type: str) -> None:
    """Mirror detached lifecycle into canonical PostgreSQL control-plane tables."""
    from sqlalchemy import func, select
    from app.database import SessionLocal as PgeSession
    from app.models import HermesGoal
    from control_plane.database import SessionLocal as ControlSession, create_schema
    from control_plane.models import RunEventRecord, RunRecord

    project_id = record["project_id"]
    with PgeSession() as pge_db:
        goal = (pge_db.query(HermesGoal)
                .filter(HermesGoal.project_id == project_id,
                        HermesGoal.status.in_(("active", "proposed")))
                .order_by(HermesGoal.created_at.desc()).first())
    if goal is None:
        return

    create_schema()
    with ControlSession() as db:
        run = db.get(RunRecord, record["run_id"])
        if run is None:
            run = RunRecord(
                id=record["run_id"], project_id=project_id, goal_id=goal.id,
                provider_id="lmstudio", max_turns=int(os.getenv("PGE_MAX_TURNS", "24")),
            )
            db.add(run)
            db.flush()
        run.status = record.get("status", run.status)
        run.turn = int(record.get("batch", run.turn or 0))
        run.heartbeat_at = datetime.now(timezone.utc)
        run.terminal_reason = record.get("terminal_reason") or record.get("failure")
        sequence = db.scalar(select(
            func.coalesce(func.max(RunEventRecord.sequence), 0) + 1
        ).where(RunEventRecord.run_id == run.id))
        db.add(RunEventRecord(
            run_id=run.id, sequence=sequence, event_type=event_type,
            actor="pge-supervisor", payload={
                key: value for key, value in record.items()
                if key not in {"traceback"} and isinstance(value, (str, int, float, bool, dict, list, type(None)))
            },
        ))
        db.commit()


def update_run(project_id: str, run_id: str, **changes: Any) -> bool:
    """Update only the matching run, preventing an old child overwriting a new run."""
    state = load_run_state()
    current = state.get(project_id)
    if not current or current.get("run_id") != run_id:
        return False
    current.update(changes)
    current["updated_at"] = _now()
    state[project_id] = current
    save_run_state(state)
    _persist_lifecycle(current, f"run.{current.get('status', 'updated')}")
    return True


def launch_pge(project_id: str, source: str, invocation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Launch PGE independently of the gateway and verify that startup succeeded."""
    if not project_id:
        return {"status": "error", "message": "project_id is required"}

    state = load_run_state()
    existing = state.get(project_id, {})
    if process_is_alive(existing.get("pid")):
        return {"status": "success", "started": False, "already_running": True, **existing}

    run_id = str(uuid.uuid4())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUN_DIR / f"{stamp}_{project_id[:8]}_{run_id[:8]}.log"
    record = {
        "run_id": run_id,
        "project_id": project_id,
        "source": source,
        "status": "launching",
        "started_at": _now(),
        "updated_at": _now(),
        "log": str(log_path),
        "launcher_pid": os.getpid(),
        "invocation": invocation or {},
    }
    state[project_id] = record
    save_run_state(state)
    _persist_lifecycle(record, "run.launching")

    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [str(PYTHON), "-u", str(ROOT / "run_pge.py"), "--project", project_id,
                 "--run-id", run_id],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=ROOT,
                env={
                    **os.environ,
                    "LLM_MODEL": os.getenv("LLM_MODEL", "google/gemma-4-12b-qat"),
                    "PGE_PLANNER_MODEL": os.getenv("PGE_PLANNER_MODEL", "omnicoder-9b-q8"),
                    "PGE_EXECUTOR_MODEL": os.getenv("PGE_EXECUTOR_MODEL", "omnicoder-9b-q8"),
                },
                start_new_session=True,
                close_fds=True,
            )
        update_run(project_id, run_id, pid=proc.pid, process_group=proc.pid, status="starting")
        time.sleep(0.35)
        exit_code = proc.poll()
        if exit_code is not None:
            update_run(project_id, run_id, status="failed", exit_code=exit_code,
                       finished_at=_now(), failure="runner exited during startup")
            return {"status": "error", "message": "PGE runner exited during startup",
                    "exit_code": exit_code, "run_id": run_id, "log": str(log_path)}
    except Exception as exc:
        update_run(project_id, run_id, status="failed", finished_at=_now(), failure=str(exc))
        return {"status": "error", "message": str(exc), "run_id": run_id, "log": str(log_path)}

    return {"status": "success", "started": True, "run_id": run_id, "pid": proc.pid,
            "log": str(log_path), "source": source,
            "note": "Detached PGE supervisor started; lifecycle is durable in PostgreSQL."}
