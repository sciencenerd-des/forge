"""Eval suite runner: runs every goal in evals/suite/manifest.json through the
harness and aggregates the five gate metrics into evals/results/<git-sha>.json.

Requires a configured LLM backend (see .env.example) and Postgres to actually
drive goals through run_pge; without one it reports which goals it could not
run rather than fabricating a result, so a missing backend degrades to a
truthful partial report instead of a false green.

Verification is *not* an exit-0 check. Every produced workspace is judged by the
per-goal acceptance contract in ``evals/acceptance.py``: the workspace is copied
out of the agent's sandbox and re-run with exact commands, so ``outcome ==
"verified"`` means real behavior/artifacts were reproduced, and a goal the agent
declared done but the contract rejects is recorded as a false completion.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evals import gate
from evals.acceptance import verify_manifest_goal

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evals" / "suite" / "manifest.json"
RESULTS_DIR = ROOT / "evals" / "results"

# contract verdict -> (gate outcome, whether a claimed-done goal is a false completion)
_VERDICT_TO_OUTCOME = {
    "accepted": (gate.VERIFIED, False),
    "rejected": (gate.COMPLETE_UNVERIFIED, True),
    "unverifiable": (gate.BLOCKED, False),
    "error": (gate.ERROR, False),
}


@dataclass
class GoalRun:
    goal_id: str
    outcome: str  # "verified" | "complete_unverified" | "blocked" | "error" | "skipped"
    # Convergence/cost metrics are null until read from the durable ledger. They
    # are NEVER 0: a favorable zero (0 cycles, 0 cost) silently games the gate.
    cycles: int | None = None
    distance_auc: float | None = None
    false_completion: bool = False
    token_cost: int | None = None
    measurement_unavailable_reason: str | None = "convergence ledger not read by this runner"
    note: str = ""


@dataclass
class SuiteResult:
    git_sha: str
    verified_completion_rate: float
    mean_cycles_to_done: float | None
    mean_distance_auc: float | None
    false_completion_count: int
    token_cost: int | None
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


def verify_workspace(goal_id: str, workspace: str | Path, *, claimed_done: bool = True) -> GoalRun:
    """Judge a produced workspace by its acceptance contract.

    This is the verification seam, independent of how the workspace was created,
    so it is unit-testable without a live model. ``claimed_done`` records whether
    the agent reported the goal finished — a claimed-done goal the contract
    rejects is a false completion (the reward channel lied).
    """
    result = verify_manifest_goal(goal_id, workspace)
    outcome, is_false = _VERDICT_TO_OUTCOME.get(result["verdict"], (gate.ERROR, False))
    false_completion = bool(claimed_done) and is_false
    return GoalRun(goal_id=goal_id, outcome=outcome, false_completion=false_completion,
                   note=result["reason"])


def run_goal(goal: dict) -> GoalRun:
    if not backend_configured():
        return GoalRun(
            goal_id=goal["id"], outcome=gate.SKIPPED,
            note="no LLM_BASE_URL/DATABASE_URL configured; run `make setup && make db` first",
        )
    workspace, claimed_done, note = _drive_goal(goal)
    if workspace is None:
        return GoalRun(goal_id=goal["id"], outcome=gate.ERROR, note=note)
    # Convergence metrics (cycles/distance_auc/token_cost) come from the Phase 1
    # ledger, which this thin runner does not read; the authoritative pass/fail
    # is the contract verdict below.
    return verify_workspace(goal["id"], workspace, claimed_done=claimed_done)


def _drive_goal(goal: dict) -> tuple[Path | None, bool, str]:
    """Run a manifest goal through run_pge and return (workspace, claimed_done, note).

    ``claimed_done`` is whether run_pge exited 0 (the agent's own completion
    claim); the acceptance contract, not this flag, decides the real outcome.
    """
    import os
    import uuid

    project = f"eval-{goal['id']}-{uuid.uuid4().hex[:8]}"
    command = [sys.executable, "run_pge.py", "--new-project", project,
               "--goal", goal["description"], "--desc", goal.get("title", goal["description"])]
    try:
        proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                              timeout=int(os.environ.get("SUITE_GOAL_TIMEOUT", "900")))
    except subprocess.TimeoutExpired:
        return None, False, "run_pge timed out before producing a workspace"
    workspace = _resolve_workspace(project)
    if workspace is None:
        return None, proc.returncode == 0, f"run_pge produced no locatable workspace (rc={proc.returncode})"
    return workspace, proc.returncode == 0, ""


def _resolve_workspace(project: str) -> Path | None:
    """Find the workspace run_pge created for ``project`` via the project row."""
    try:
        from app.database import SessionLocal
        from app.models import ForgeProject
    except Exception:
        return None
    with SessionLocal() as db:
        row = (db.query(ForgeProject).filter(ForgeProject.name == project)
               .order_by(ForgeProject.created_at.desc()).first())
        if row and row.repo_path and Path(row.repo_path).exists():
            return Path(row.repo_path)
    return None


def run_suite() -> SuiteResult:
    manifest = json.loads(MANIFEST.read_text())
    goals = manifest["goals"]
    runs = [run_goal(g) for g in goals]

    verified = [r for r in runs if r.outcome == "verified"]
    rate = len(verified) / len(runs) if runs else 0.0
    false_completions = sum(1 for r in runs if r.false_completion)

    def _mean(values: list[int | float | None]) -> float | None:
        # An aggregate over unmeasured samples is itself unmeasured — null, not 0.
        present = [v for v in values if v is not None]
        if not present or len(present) != len(values):
            return None
        return sum(present) / len(present)

    def _total(values: list[int | None]) -> int | None:
        present = [v for v in values if v is not None]
        if not present or len(present) != len(values):
            return None
        return sum(present)

    mean_cycles = _mean([r.cycles for r in verified]) if verified else None
    mean_auc = _mean([r.distance_auc for r in verified]) if verified else None
    total_tokens = _total([r.token_cost for r in runs])

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
