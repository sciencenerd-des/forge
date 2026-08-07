"""Canonical eight-goal catalogue and thin orchestrator adapter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.orchestrator import run_production_suite, suite_exit_code

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


def run_suite(*, model: str, timeout: int, max_turns: int, output: Path,
              only: int | None = None, base_url: str | None = None) -> dict:
    """Delegate suite execution to the same production path as the TUI CLI."""
    from evals.orchestrator import build_provider_environment
    from forge_config import DEFAULT_BASE_URL

    selected_url = base_url or os.getenv("FORGE_LLM_BASE_URL", DEFAULT_BASE_URL)
    return run_production_suite(
        goals=GOALS, model=model, base_url=selected_url, timeout=timeout,
        max_turns=max_turns, output=output, only=only,
        child_env=build_provider_environment(
            model=model, base_url=selected_url, max_turns=max_turns
        ),
        suite_hash="canonical-eight-goal-suite",
    )


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--timeout", type=int, default=int(float(os.getenv("SUITE_GOAL_TIMEOUT_MINUTES", "30")) * 60))
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--output", type=Path, default=Path("evals/results/goal-suite.json"))
    parser.add_argument("--only", type=int, choices=range(1, 9))
    args = parser.parse_args()
    report = run_suite(model=args.model, base_url=args.base_url, timeout=args.timeout,
                       max_turns=args.max_turns, output=args.output, only=args.only)
    print(json.dumps(report, indent=2))
    return suite_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
