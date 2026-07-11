"""Eval gate: compares a candidate-branch benchmark result to the baseline.

Merges only proposed harness-config changes (see the whitelist below) whose
benchmark result does not regress on any metric where lower/higher is better,
and hard-fails on ANY false completion — a single false completion means the
reward channel lied, which is a categorically worse outcome than a slow run.

Usage:
    python evals/gate.py --candidate evals/results/<sha>.json --baseline evals/results/baseline.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Only these harness knobs may be proposed/merged by the self-improvement
# loop (Phase 6). Anything else — in particular engine code — is out of
# scope for automated proposals; see docs/CONVERGENCE.md.
CONFIG_WHITELIST = {
    "controller_epsilon",
    "lesson_pack_budget_chars",
    "action_search_n",
    "prompt_template_variant",
}

# metric_name -> True if lower is better
METRIC_DIRECTIONS = {
    "verified_completion_rate": False,  # higher is better
    "mean_cycles_to_done": True,
    "mean_distance_auc": True,
    "false_completion_count": True,
    "token_cost": True,
}


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]
    deltas: dict


def evaluate_gate(candidate: dict, baseline: dict) -> GateResult:
    reasons: list[str] = []
    deltas: dict = {}

    candidate_goals = candidate.get("goals")
    baseline_goals = baseline.get("goals")
    if not isinstance(candidate_goals, list) or not candidate_goals:
        reasons.append("candidate has no per-goal benchmark evidence")
    if not isinstance(baseline_goals, list) or not baseline_goals:
        reasons.append("baseline has no per-goal benchmark evidence")

    if isinstance(candidate_goals, list) and candidate_goals:
        incomplete = [
            str(goal.get("goal_id", "unknown"))
            for goal in candidate_goals
            if not isinstance(goal, dict) or goal.get("outcome") != "verified"
        ]
        if incomplete:
            reasons.append(
                "candidate has incomplete benchmark goals: " + ", ".join(incomplete)
            )

    if isinstance(candidate_goals, list) and isinstance(baseline_goals, list):
        candidate_ids = {
            str(goal.get("goal_id")) for goal in candidate_goals if isinstance(goal, dict)
        }
        baseline_ids = {
            str(goal.get("goal_id")) for goal in baseline_goals if isinstance(goal, dict)
        }
        if candidate_ids != baseline_ids:
            reasons.append("candidate and baseline benchmark goal sets differ")

    cand_false = candidate.get("false_completion_count", 0)
    if cand_false > 0:
        reasons.append(f"hard-fail: candidate has {cand_false} false completion(s)")

    for metric, lower_is_better in METRIC_DIRECTIONS.items():
        if metric not in candidate or metric not in baseline:
            continue
        c, b = candidate[metric], baseline[metric]
        deltas[metric] = c - b
        regressed = (c > b) if lower_is_better else (c < b)
        if regressed and metric != "false_completion_count":
            reasons.append(f"regression on {metric}: baseline={b} candidate={c}")

    return GateResult(passed=not reasons, reasons=tuple(reasons), deltas=deltas)


def validate_whitelist(proposed_changes: dict) -> tuple[bool, tuple[str, ...]]:
    """A proposer may only touch whitelisted config knobs."""
    rejected = tuple(k for k in proposed_changes if k not in CONFIG_WHITELIST)
    return (not rejected, rejected)


def render_report(result: GateResult) -> str:
    lines = ["| metric | delta |", "| --- | --- |"]
    for metric, delta in result.deltas.items():
        lines.append(f"| {metric} | {delta:+.4f} |")
    verdict = "PASS" if result.passed else "FAIL"
    header = f"## Eval gate: {verdict}\n"
    if result.reasons:
        header += "\n".join(f"- {r}" for r in result.reasons) + "\n\n"
    return header + "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    args = parser.parse_args()

    candidate = json.loads(args.candidate.read_text())
    baseline = json.loads(args.baseline.read_text())
    result = evaluate_gate(candidate, baseline)
    print(render_report(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
