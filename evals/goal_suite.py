"""Run the canonical eight-goal PGE suite in isolated projects."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.database import SessionLocal
from app.models import ForgeFileChange, ForgeGoal, ForgeProject, ForgeTestRun

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
    """Classify durable evidence after a subprocess exits."""
    with SessionLocal() as db:
        project_row = db.query(ForgeProject).filter(ForgeProject.name == project).order_by(ForgeProject.created_at.desc()).first()
        project_id = project_row.id if project_row else None
        goal = db.query(ForgeGoal).filter(ForgeGoal.project_id == project_id).order_by(ForgeGoal.created_at.desc()).first() if project_id else None
        changes = db.query(ForgeFileChange).filter(ForgeFileChange.project_id == project_id).count() if project_id else 0
        latest_test = db.query(ForgeTestRun).filter(ForgeTestRun.project_id == project_id).order_by(ForgeTestRun.created_at.desc()).first() if project_id else None
        return {"slug": slug, "goal_status": goal.status if goal else None,
                "file_changes": changes, "latest_test_status": latest_test.status if latest_test else None}


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
            completed = subprocess.run(command, cwd=Path(__file__).parents[1], env=env,
                                       text=True, capture_output=True, timeout=timeout)
            status = "completed" if completed.returncode == 0 else "failed"
            error = completed.stderr[-4000:] if completed.returncode else None
        except subprocess.TimeoutExpired as exc:
            status, error = "timeout", str(exc)
        durable = _verdict(project, slug)
        if status == "completed" and durable.get("goal_status") == "blocked":
            status = "blocked"
        results.append({"slug": slug, "goal": goal, "project": project,
                        "status": status, "elapsed_s": round(time.monotonic() - begin, 2),
                        "error": error, **durable})
    report = {"model": model, "goals": results,
              "completed": sum(item["status"] == "completed" for item in results),
              "total": len(results), "elapsed_s": round(time.monotonic() - started, 2),
              "summary": {"completed": sum(item["status"] == "completed" for item in results),
                          "failed": sum(item["status"] == "failed" for item in results),
                          "blocked": sum(item["status"] == "blocked" for item in results),
                          "timeout": sum(item["status"] == "timeout" for item in results)}}
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
