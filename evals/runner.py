"""Eval suite runner: runs every goal in evals/suite/manifest.json through the
harness and aggregates the five gate metrics into evals/results/<git-sha>.json.

Requires a configured LLM backend (see .env.example) and Postgres to actually
drive goals through run_pge; without one it reports which goals it could not
run rather than fabricating a result, so a missing backend degrades to a
truthful partial report instead of a false green.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evals" / "suite" / "manifest.json"
RESULTS_DIR = ROOT / "evals" / "results"


@dataclass
class GoalRun:
    goal_id: str
    outcome: str  # "verified" | "complete_unverified" | "blocked" | "error" | "skipped"
    cycles: int = 0
    distance_auc: float = 0.0
    false_completion: bool = False
    token_cost: int = 0
    note: str = ""


@dataclass
class SuiteResult:
    git_sha: str
    verified_completion_rate: float
    mean_cycles_to_done: float
    mean_distance_auc: float
    false_completion_count: int
    token_cost: int
    goals: list = field(default_factory=list)


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def backend_configured() -> bool:
    """Best-effort check that a run_pge-capable backend is reachable.

    Kept intentionally conservative: absence of DATABASE_URL/LLM config means
    we cannot truthfully claim a run happened, so callers must skip rather
    than fabricate a result.
    """
    import os

    return bool(os.environ.get("DATABASE_URL") and os.environ.get("LLM_BASE_URL"))


def run_goal(goal: dict) -> GoalRun:
    if not backend_configured():
        return GoalRun(
            goal_id=goal["id"], outcome="skipped",
            note="no LLM_BASE_URL/DATABASE_URL configured; run `make setup && make db` first",
        )
    # Real invocation would call run_pge(...) here and translate the resulting
    # snapshot (convergence ledger from Phase 1, verification tier from
    # Phase 3) into a GoalRun. Left as an integration point: wiring this to
    # a live model in CI is out of scope for this pass.
    return GoalRun(goal_id=goal["id"], outcome="skipped", note="live harness invocation not wired in this environment")


def run_suite() -> SuiteResult:
    manifest = json.loads(MANIFEST.read_text())
    goals = manifest["goals"]
    runs = [run_goal(g) for g in goals]

    verified = [r for r in runs if r.outcome == "verified"]
    rate = len(verified) / len(runs) if runs else 0.0
    false_completions = sum(1 for r in runs if r.false_completion)
    mean_cycles = sum(r.cycles for r in verified) / len(verified) if verified else 0.0
    mean_auc = sum(r.distance_auc for r in verified) / len(verified) if verified else 0.0
    total_tokens = sum(r.token_cost for r in runs)

    return SuiteResult(
        git_sha=_git_sha(),
        verified_completion_rate=rate,
        mean_cycles_to_done=mean_cycles,
        mean_distance_auc=mean_auc,
        false_completion_count=false_completions,
        token_cost=total_tokens,
        goals=[asdict(r) for r in runs],
    )


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result = run_suite()
    out_path = RESULTS_DIR / f"{result.git_sha}.json"
    out_path.write_text(json.dumps(asdict(result), indent=2))
    print(f"wrote {out_path}")
    skipped = [g for g in result.goals if g["outcome"] == "skipped"]
    if skipped:
        print(
            f"{len(skipped)}/{len(result.goals)} goals skipped — benchmark is incomplete; see notes",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
