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


def run_suite(*, model: str, timeout: int, max_turns: int, output: Path) -> dict[str, Any]:
    started = time.monotonic()
    results = []
    for slug, goal in GOALS:
        project = f"goal-suite-{slug}-{uuid.uuid4().hex[:8]}"
        env = os.environ.copy()
        env["PGE_MAX_TURNS"] = str(max_turns)
        env["LLM_MODEL"] = model
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
        results.append({"slug": slug, "goal": goal, "project": project,
                        "status": status, "elapsed_s": round(time.monotonic() - begin, 2),
                        "error": error})
    report = {"model": model, "goals": results,
              "completed": sum(item["status"] == "completed" for item in results),
              "total": len(results), "elapsed_s": round(time.monotonic() - started, 2)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_suite(model=args.model, timeout=args.timeout,
                       max_turns=args.max_turns, output=args.output)
    print(json.dumps(report, indent=2))
    return 0 if report["completed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
