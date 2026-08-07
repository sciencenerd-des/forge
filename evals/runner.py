"""Thin manifest-suite adapter over :mod:`evals.orchestrator`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import gate
from evals.acceptance import verify_manifest_goal
from evals.orchestrator import run_production_suite, suite_exit_code

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evals" / "suite" / "manifest.json"
RESULTS_DIR = ROOT / "evals" / "results"


@dataclass
class GoalRun:
    goal_id: str
    outcome: str
    false_completion: bool = False
    note: str = ""


def verify_workspace(goal_id: str, workspace: str | Path, *, claimed_done: bool = True) -> GoalRun:
    """Compatibility unit-test seam; live runner verification is containerized."""
    result = verify_manifest_goal(goal_id, workspace)
    verdict = result.get("verdict")
    if verdict == "accepted":
        outcome, false = gate.VERIFIED, False
    elif verdict == "rejected":
        outcome, false = gate.COMPLETE_UNVERIFIED, bool(claimed_done)
    elif verdict == "unverifiable":
        outcome, false = gate.BLOCKED, False
    else:
        outcome, false = gate.ERROR, False
    return GoalRun(goal_id=goal_id, outcome=outcome, false_completion=false,
                   note=result.get("reason", ""))


def manifest_goals() -> list[tuple[str, str]]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [(str(goal["id"]), str(goal.get("description") or goal.get("title") or goal["id"]))
            for goal in payload["goals"]]


def run_suite(*, model: str, base_url: str, timeout: int, max_turns: int,
              output: Path, only: int | None = None) -> dict:
    from evals.acceptance import ContainerRunner, verify_manifest_goal

    def verify(goal_id: str, snapshot_path: str | None) -> dict:
        if snapshot_path is None:
            return {"verdict": "unverifiable", "reason": "no snapshot", "gateable": True}
        return verify_manifest_goal(goal_id, snapshot_path, runner=ContainerRunner())

    from evals.orchestrator import build_provider_environment

    return run_production_suite(
        goals=manifest_goals(), model=model, base_url=base_url,
        timeout=timeout, max_turns=max_turns, output=output, only=only,
        child_env=build_provider_environment(
            model=model, base_url=base_url, max_turns=max_turns
        ),
        verify_override=verify, suite_hash="manifest-suite",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=os.getenv("FORGE_LLM_BASE_URL", "http://localhost:1234/v1"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("SUITE_GOAL_TIMEOUT", "900")))
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--only", type=int, choices=range(1, len(manifest_goals()) + 1))
    args = parser.parse_args()
    output = args.output or RESULTS_DIR / "orchestrator-manifest-v2.json"
    report = run_suite(model=args.model, base_url=args.base_url, timeout=args.timeout,
                       max_turns=args.max_turns, output=output, only=args.only)
    print(json.dumps(report, indent=2))
    return suite_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
