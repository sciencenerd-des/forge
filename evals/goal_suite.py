"""Run the canonical eight-goal PGE suite in isolated projects."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.database import SessionLocal
from app.models import ForgeFileChange, ForgeGoal, ForgeProject, ForgeTestRun
from evals import gate
from evals.acceptance import verify_goal

GOALS = [
    ("snake", "Build a playable Snake game in Python using curses."),
    ("digits", "Build a sklearn digits experiment loop and save results.json with accuracy above 0.95."),
    ("lru", "Implement an LRU cache with O(1) get and put operations."),
    ("rpn", "Build a robust reverse Polish notation calculator."),
    ("json-parser", "Implement a JSON parser from scratch without importing the json module."),
    ("tic-tac-toe", "Build a playable tic-tac-toe game with win and draw detection."),
    ("dijkstra", "Implement Dijkstra shortest paths with useful tests."),
    ("email-validator", "Build a JavaScript email validator with tests."),
]


def _verdict(project: str, slug: str) -> dict[str, Any]:
    """Classify durable evidence after a subprocess exits.

    The DB rows (goal status, a "success" test row, a file-change count) only
    record what the agent *claimed*. The authoritative signal is the acceptance
    contract in ``evals/acceptance.py``: it copies the workspace out of the
    agent's sandbox and re-verifies real behavior/artifacts. ``accepted`` from
    that contract is the only true pass; everything else is surfaced verbatim so
    a false completion cannot masquerade as green.
    """
    with SessionLocal() as db:
        project_row = db.query(ForgeProject).filter(ForgeProject.name == project).order_by(ForgeProject.created_at.desc()).first()
        project_id = project_row.id if project_row else None
        repo_path = project_row.repo_path if project_row else None
        goal = db.query(ForgeGoal).filter(ForgeGoal.project_id == project_id).order_by(ForgeGoal.created_at.desc()).first() if project_id else None
        changes = db.query(ForgeFileChange).filter(ForgeFileChange.project_id == project_id).count() if project_id else 0
        latest_test = db.query(ForgeTestRun).filter(ForgeTestRun.project_id == project_id).order_by(ForgeTestRun.created_at.desc()).first() if project_id else None
    acceptance = verify_goal(slug, repo_path)
    return {"slug": slug, "goal_status": goal.status if goal else None,
            "file_changes": changes, "latest_test_status": latest_test.status if latest_test else None,
            "repo_path": repo_path,
            "acceptance_verdict": acceptance["verdict"],
            "accepted": acceptance["accepted"],
            "acceptance_reason": acceptance["reason"],
            "acceptance": acceptance}


STATUS_KINDS = ("completed", "failed", "blocked", "timeout", "rejected", "unverified", "inconsistent")

# The suite's internal status vocabulary mapped onto the shared gate outcome
# vocabulary (evals/gate.py), so a goal-suite report is consumable by the same
# gate as evals/runner.py output.
STATUS_TO_OUTCOME = {
    "completed": gate.VERIFIED,
    "rejected": gate.COMPLETE_UNVERIFIED,
    "unverified": gate.BLOCKED,
    "blocked": gate.BLOCKED,
    "timeout": gate.BLOCKED,
    "failed": gate.ERROR,
    "inconsistent": gate.ERROR,
    "skipped": gate.SKIPPED,
}


def reconcile_status(status: str, durable: dict[str, Any]) -> str:
    """Fold the acceptance verdict into a launcher/subprocess status.

    A process that exited 0 only *claims* completion. The acceptance contract's
    independent re-run is the arbiter:

    * accepted           -> stays ``completed`` (a verified completion);
    * rejected           -> ``rejected`` (a false completion — the reward lied);
    * unverifiable/error -> ``unverified`` (honest non-pass; we could not
      reproduce it, but that is not proof of a lie).
    """
    if status != "completed":
        return status
    verdict = durable.get("acceptance_verdict")
    if verdict == "accepted":
        return "completed"
    if verdict == "rejected":
        return "rejected"
    return "unverified"


def outcome_fields(status: str) -> tuple[str, bool]:
    """Return (gate outcome, is_false_completion) for a final status."""
    return STATUS_TO_OUTCOME.get(status, gate.ERROR), status == "rejected"


def _build_report(model: str, results: list[dict[str, Any]], started: float) -> dict[str, Any]:
    """Aggregate per-goal results into a report.

    ``completed`` counts only acceptance-verified goals (the status downgrade
    guarantees a "completed" row was accepted). ``false_completion_count`` counts
    goals the agent declared done but the contract rejected — the metric
    ``evals/gate.py`` hard-fails on, because a lying reward channel is worse than
    a slow run.
    """
    def count(kind: str) -> int:
        return sum(item["status"] == kind for item in results)

    core = ("completed", "failed", "blocked", "timeout", "rejected", "unverified")
    summary = {kind: count(kind) for kind in STATUS_KINDS
               if kind in core or count(kind)}
    return {"model": model, "goals": results,
            "completed": count("completed"),
            "verified_completion_rate": (count("completed") / len(results)) if results else 0.0,
            "false_completion_count": sum(bool(item.get("false_completion")) for item in results),
            "total": len(results), "elapsed_s": round(time.monotonic() - started, 2),
            "summary": summary}


def run_suite(*, model: str, timeout: int, max_turns: int, output: Path,
              only: int | None = None) -> dict[str, Any]:
    started = time.monotonic()
    results = []
    selected = GOALS if only is None else [GOALS[only - 1]]
    for index, (slug, goal) in enumerate(selected, start=1):
        project = f"suite-{index}-{slug}-{uuid.uuid4().hex[:8]}"
        env = os.environ.copy()
        env["PGE_MAX_TURNS"] = str(max_turns)
        env["LLM_MODEL"] = model
        env.setdefault("DATABASE_URL", "postgresql://forge:forge@localhost:5434/forge")
        begin = time.monotonic()
        command = [sys.executable, "run_pge.py", "--new-project", project,
                   "--goal", goal, "--desc", goal]
        try:
            process = subprocess.Popen(command, cwd=Path(__file__).parents[1], env=env,
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc
            completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            status = "completed" if completed.returncode == 0 else "failed"
            error = completed.stderr[-4000:] if completed.returncode else None
        except subprocess.TimeoutExpired as exc:
            status, error = "timeout", str(exc)
        durable = _verdict(project, slug)
        if status == "completed" and durable.get("goal_status") == "blocked":
            status = "blocked"
        # A process that exited 0 is not a pass: only the acceptance contract's
        # independent re-run can keep a completion. Rejected -> false completion;
        # unverifiable/error on a claimed completion -> honest "unverified".
        status = reconcile_status(status, durable)
        if status in ("rejected", "unverified"):
            error = error or durable.get("acceptance_reason")
        outcome, false_completion = outcome_fields(status)
        results.append({"slug": slug, "goal": goal, "project": project,
                        "goal_id": slug, "outcome": outcome, "false_completion": false_completion,
                        "status": status, "elapsed_s": round(time.monotonic() - begin, 2),
                        "error": error, "turns_used": None, **durable})
    report = _build_report(model, results, started)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=int(float(os.getenv("SUITE_GOAL_TIMEOUT_MINUTES", "30")) * 60))
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--output", type=Path, default=Path("evals/results/goal-suite-gemma4-12b-mlx.json"))
    parser.add_argument("--only", type=int, choices=range(1, 9))
    args = parser.parse_args()
    report = run_suite(model=args.model, timeout=args.timeout,
                       max_turns=args.max_turns, output=args.output, only=args.only)
    print(json.dumps(report, indent=2))
    return 0 if report["completed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
