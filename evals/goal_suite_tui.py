"""TUI-facing thin adapter for the canonical production eval orchestrator."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.goal_suite import GOALS
from evals.orchestrator import (
    build_provider_environment,
    run_production_suite,
    suite_exit_code,
)
from forge_config import DEFAULT_BASE_URL


def run_suite(
    *,
    model: str,
    base_url: str,
    timeout: int,
    max_turns: int,
    output: Path,
    only: int | None = None,
) -> dict:
    """Delegate without owning launch, polling, snapshot, or verification."""
    return run_production_suite(
        goals=GOALS,
        model=model,
        base_url=base_url,
        timeout=timeout,
        max_turns=max_turns,
        output=output,
        only=only,
        child_env=build_provider_environment(
            model=model, base_url=base_url, max_turns=max_turns
        ),
        suite_hash="canonical-eight-goal-suite",
    )


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(float(os.getenv("SUITE_GOAL_TIMEOUT_MINUTES", "30")) * 60),
    )
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument(
        "--output", type=Path, default=Path("evals/results/goal-suite-tui.json")
    )
    parser.add_argument("--only", type=int, choices=range(1, 9))
    args = parser.parse_args()
    report = run_suite(
        model=args.model,
        base_url=args.base_url,
        timeout=args.timeout,
        max_turns=args.max_turns,
        output=args.output,
        only=args.only,
    )
    print(json.dumps(report, indent=2))
    return suite_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
