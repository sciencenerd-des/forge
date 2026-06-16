"""Planner -> Generator(Executor) -> Evaluator autonomous loop runner.

Persistence model: the Postgres DB *is* the durable state. Goals, tasks and
their statuses live in `hermes_*` tables, so re-running this script resumes
the work in progress — the planner reloads the open tasks and continues. The
graph itself terminates cleanly (turn ceiling / no actionable tasks / blocked)
instead of spinning, so you do not have to babysit it.

Usage:
    python run_pge.py                         # resume default project
    python run_pge.py --project <uuid> --goal "Build X" --desc "..."
    PGE_MAX_TURNS=20 python run_pge.py
"""
import argparse
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

# --- Make both package roots importable (the layout splits `app` from `src`) ---
_ROOT = os.path.dirname(os.path.abspath(__file__))
_AUTONOMY = os.path.join(_ROOT, "engine")
for p in (_ROOT, _AUTONOMY):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv

load_dotenv()

from src.graph import MAX_TURNS
from src.graph import app as pge_graph  # noqa: E402
from src.runtime import active_goal_query  # noqa: E402
from src.state.schema import Goal  # noqa: E402

import forge_config  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (
    HermesFileChange,
    HermesMemoryItem,
    HermesTask,
    HermesTestRun,  # noqa: E402
)
from app.services import MemoryService  # noqa: E402
from pge_launcher import load_run_state, update_run  # noqa: E402


def _utcnow() -> datetime:
    """Naive UTC now. DB columns are TIMESTAMP WITHOUT TIME ZONE, so we keep
    timestamps naive while avoiding the deprecated ``datetime.utcnow()``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_default_project() -> str:
    """Resolve (or create) the default project id — never a hardcoded UUID.
    Honors FORGE_DEFAULT_PROJECT; otherwise resolve-or-create a 'default'
    project so the loop runs out of the box on any machine."""
    db = SessionLocal()
    try:
        return forge_config.ensure_default_project(db)
    finally:
        db.close()


def _load_progress(project_id: str):
    db = SessionLocal()
    try:
        goal = active_goal_query(db, project_id)
        if not goal:
            return None, ()
        tasks = db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == goal.id,
        ).order_by(HermesTask.created_at.asc()).all()
        task_ids = [t.id for t in tasks]
        evidence = (0, None, 0, None, 0, None)
        if task_ids:
            file_count = db.query(HermesFileChange).filter(
                HermesFileChange.task_id.in_(task_ids)).count()
            latest_file = db.query(HermesFileChange.created_at).filter(
                HermesFileChange.task_id.in_(task_ids)).order_by(
                    HermesFileChange.created_at.desc()).first()
            test_count = db.query(HermesTestRun).filter(
                HermesTestRun.task_id.in_(task_ids)).count()
            latest_test = db.query(HermesTestRun.created_at).filter(
                HermesTestRun.task_id.in_(task_ids)).order_by(
                    HermesTestRun.created_at.desc()).first()
            memory_count = db.query(HermesMemoryItem).filter(
                HermesMemoryItem.task_id.in_(task_ids),
                HermesMemoryItem.status == "active").count()
            latest_memory = db.query(HermesMemoryItem.updated_at).filter(
                HermesMemoryItem.task_id.in_(task_ids),
                HermesMemoryItem.status == "active").order_by(
                    HermesMemoryItem.updated_at.desc()).first()
            evidence = (
                file_count, latest_file[0] if latest_file else None,
                test_count, latest_test[0] if latest_test else None,
                memory_count, latest_memory[0] if latest_memory else None,
            )
        fingerprint = (
            goal.id,
            goal.status,
            tuple((t.id, t.status, t.description or "") for t in tasks),
            evidence,
        )
        return goal, fingerprint
    finally:
        db.close()


def _initial_state(project_id: str, goal=None):
    runtime_goal = None
    if goal:
        runtime_goal = Goal(
            id=goal.id, title=goal.title, description=goal.description or "",
            status=goal.status, success_criteria=goal.success_criteria or [],
            priority=goal.priority,
        )
    return {
        "project_id": project_id,
        "goal": runtime_goal,
        "task_queue": [],
        "active_task": None,
        "heartbeat": None,
        "history": [],
        "decision": "continue",
        "last_eval": None,
        "task_attempts": {},
        "recent_action_signatures": [],
        "selected_lesson_ids": [],
        "steering_reason": None,
        "goal_verified": False,
        "turn_count": 0,
        "current_run_id": str(uuid.uuid4()),
        "timestamp": _utcnow(),
    }


def run_pge(project_id: str, goal_title: str = None, goal_desc: str = None,
            run_id: str | None = None):
    if run_id:
        update_run(project_id, run_id, status="running", runner_pid=os.getpid(),
                   heartbeat_at=_utcnow().isoformat())
    print(f"--- PGE loop start | project={project_id} | max_turns={MAX_TURNS} ---")
    sync_db = SessionLocal()
    try:
        sync_result = MemoryService(sync_db).sync_sqlite_to_postgres()
        print(f"--- Gateway state mirror | {sync_result} ---")
    finally:
        sync_db.close()

    goal = None
    if goal_title:
        goal = Goal(
            id=str(uuid.uuid4()),
            title=goal_title,
            description=goal_desc or goal_title,
            status="active",
            success_criteria=["Task produces a verifiable file change or successful test run"],
            priority=1,
        )

    config = {"recursion_limit": MAX_TURNS * 3 + 10}
    stagnant_batches = 0
    max_stagnant_batches = int(os.getenv("PGE_MAX_STAGNANT_BATCHES", "2"))
    previous_fingerprint = None
    batch = 0
    final_state = None
    consecutive_failures = 0
    # AUTONOMY budget: a detached loop must survive BOTH model timeouts AND
    # node-level bugs — durable Postgres state means the next batch resumes
    # safely, so one bad batch must never kill the process. Only a SUSTAINED
    # streak ends the run. (Was 3 transient-only; a non-transient error used to
    # `raise` and crash instantly — the root cause of "the loop keeps dying
    # after every fix": each new node bug was a fresh fatal crash.)
    max_consecutive_failures = int(os.getenv("PGE_MAX_CONSECUTIVE_FAILURES",
                                             os.getenv("PGE_MAX_TRANSIENT_FAILURES", "6")))

    while True:
        batch += 1
        if run_id:
            update_run(project_id, run_id, status="running", batch=batch,
                       heartbeat_at=_utcnow().isoformat())
        db_goal, before = _load_progress(project_id)
        if db_goal and db_goal.status == "completed":
            print("🏁 Persisted goal is verified complete.")
            break
        state_goal = db_goal or goal
        print(f"\n--- PGE batch {batch} | durable Postgres resume ---")
        try:
            final_state = pge_graph.invoke(
                _initial_state(project_id, state_goal), config=config
            )
            consecutive_failures = 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            import traceback as _tb
            failure_name = type(exc).__name__
            failure_text = str(exc)
            tb_text = _tb.format_exc()
            transient = any(marker in f"{failure_name}: {failure_text}".lower() for marker in (
                "timeout", "connection", "temporarily unavailable", "rate limit",
                "server error", "service unavailable", "model not loaded",
                "no models loaded", "connection refused", "remotedisconnected",
                "read timed out", "bad gateway",
            ))
            db = SessionLocal()
            try:
                MemoryService(db).record_event(
                    project_id=project_id,
                    event_type="pge_transient_failure" if transient else "pge_node_failure",
                    actor="pge-supervisor",
                    content=f"{failure_name}: {failure_text}"[:4000],
                    metadata={"batch": batch, "run_id": run_id, "transient": transient,
                              "traceback": tb_text[-2000:]},
                )
            finally:
                db.close()
            # A single batch exception — transient model error OR a node-level
            # bug — must NOT kill the detached loop. The durable Postgres
            # checkpoint from the last good cycle is intact, so the next batch
            # resumes cleanly. Log it, back off, retry; only a SUSTAINED streak
            # (budget) ends the run. This is what lets the loop ride over the
            # node bugs that used to crash it outright.
            consecutive_failures += 1
            kind = "transient model" if transient else "node-level"
            print(f"⚠️  Batch {batch} hit a {kind} failure "
                  f"({consecutive_failures}/{max_consecutive_failures}): "
                  f"{failure_name}: {failure_text}")
            if not transient:
                print(tb_text)
            if consecutive_failures > max_consecutive_failures:
                if run_id:
                    update_run(project_id, run_id, status="blocked",
                               terminal_reason=f"failure_budget_exhausted:{failure_name}",
                               last_failure=f"{failure_name}: {failure_text}"[:2000],
                               heartbeat_at=_utcnow().isoformat())
                raise RuntimeError(
                    f"PGE failure budget exhausted after {consecutive_failures} "
                    f"consecutive failures (last: {failure_name}: {failure_text})"
                ) from exc
            delay = min(120, 5 * (2 ** (consecutive_failures - 1)))
            print(f"   durable state preserved; retrying from PostgreSQL in {delay}s.")
            if run_id:
                update_run(
                    project_id, run_id, status="recovering",
                    consecutive_failures=consecutive_failures,
                    last_failure=f"{failure_name}: {failure_text}"[:2000],
                    heartbeat_at=_utcnow().isoformat(),
                )
            time.sleep(delay)
            continue

        db_goal, after = _load_progress(project_id)
        if db_goal and db_goal.status == "completed":
            print("🏁 Goal completed and persisted by evaluator.")
            break

        if after == before or after == previous_fingerprint:
            stagnant_batches += 1
        else:
            stagnant_batches = 0
        previous_fingerprint = after

        decision = (final_state.get("decision") or "continue").lower()
        actionable = any(status in ("active", "proposed")
                         for _, status, _ in (after[2] if after else ()))
        if "blocked" in decision and not actionable:
            print("🚧 Durable state is blocked with no actionable task; human input required.")
            if run_id:
                update_run(project_id, run_id, status="blocked",
                           terminal_reason="blocked_no_actionable_task")
            break
        if stagnant_batches >= max_stagnant_batches:
            print(f"🛑 No durable task or evidence progress across "
                  f"{max_stagnant_batches + 1} graph batches; stopping to avoid a busy-loop.")
            if run_id:
                update_run(project_id, run_id, status="blocked",
                           terminal_reason="stagnant_durable_state")
            break
        print("↻ Batch ended before goal completion; reloading filtered Postgres state.")

    print("\n--- PGE loop finished ---")
    print(f"Turns in batch : {final_state.get('turn_count') if final_state else 0}")
    print(f"Final decision : {final_state.get('decision') if final_state else 'complete'}")
    active = final_state.get("active_task") if final_state else None
    print(f"Last task      : {getattr(active, 'title', None)} "
          f"({getattr(active, 'status', None)})")
    return final_state


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the PGE autonomy loop")
    ap.add_argument("--project", default=None,
                    help="Project UUID (omit to use/create FORGE_DEFAULT_PROJECT)")
    ap.add_argument("--goal", default=None, help="Goal title (omit to resume DB goal)")
    ap.add_argument("--desc", default=None, help="Goal description")
    ap.add_argument("--run-id", default=None, help="Lifecycle manifest run UUID")
    args = ap.parse_args()
    if not args.project:
        args.project = resolve_default_project()
    try:
        result = run_pge(args.project, args.goal, args.desc, args.run_id)
        if args.run_id:
            current_decision = (result or {}).get("decision", "complete")
            current = load_run_state().get(args.project, {})
            if current.get("run_id") == args.run_id and current.get("status") != "blocked":
                update_run(args.project, args.run_id, status="completed",
                           final_decision=current_decision,
                           finished_at=_utcnow().isoformat(), exit_code=0)
    except BaseException as exc:
        if args.run_id:
            update_run(args.project, args.run_id, status="failed",
                       failure=f"{type(exc).__name__}: {exc}",
                       traceback=traceback.format_exc()[-12000:],
                       finished_at=_utcnow().isoformat(), exit_code=1)
        raise
