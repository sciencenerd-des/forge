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

# The single per-goal outcome vocabulary shared by every producer that feeds
# this gate (evals/runner.py and evals/goal_suite.py). Keeping it here — in the
# consumer that defines the schema — is what lets both pipelines emit gate-ready
# results instead of each inventing its own goal_id/outcome shape.
VERIFIED = "verified"                    # acceptance contract passed on re-run
COMPLETE_UNVERIFIED = "complete_unverified"  # agent claimed done, contract rejected
BLOCKED = "blocked"                      # could not be verified (or never finished)
ERROR = "error"                          # crashed / harness or runtime failure
SKIPPED = "skipped"                      # not attempted (e.g. no backend)
VALID_OUTCOMES = (VERIFIED, COMPLETE_UNVERIFIED, BLOCKED, ERROR, SKIPPED)


def is_verified(outcome: str) -> bool:
    return outcome == VERIFIED


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


# v2 reports must carry these comparison metrics as real numbers; a null (an
# honest "unmeasured") makes the pair incomparable and fails the gate, rather
# than being silently treated as a favorable zero.
REQUIRED_V2_METRICS = (
    "verified_completion_rate", "mean_cycles_to_done", "mean_distance_auc", "token_cost",
)

# Verdicts that are the harness's fault, reported separately from model failures
# so infrastructure instability is never hidden inside a model "blocked".
_HARNESS_VERDICTS = {"harness_error"}


def evaluate_gate(candidate: dict, baseline: dict) -> GateResult:
    from evals.contracts import comparability_reasons, is_versioned

    reasons: list[str] = []
    deltas: dict = {}

    # Refuse to compare unlike v2 runs before doing anything else (legacy dicts
    # without a schema_version keep the historical metric-only comparison).
    reasons.extend(comparability_reasons(candidate, baseline))
    versioned = is_versioned(candidate) and is_versioned(baseline)

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
            if not isinstance(goal, dict) or not is_verified(goal.get("outcome", ""))
        ]
        if incomplete:
            reasons.append(
                "candidate has incomplete benchmark goals: " + ", ".join(incomplete)
            )
        # Report harness failures distinctly from model failures.
        harness = [
            str(goal.get("goal_id", "unknown"))
            for goal in candidate_goals
            if isinstance(goal, dict) and goal.get("outcome") in _HARNESS_VERDICTS
        ]
        if harness:
            reasons.append("candidate has harness failures: " + ", ".join(harness))

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

    # A v2 report must supply every comparison metric as a real number. Missing
    # or null is a failure to measure, not a free win.
    if versioned:
        for metric in REQUIRED_V2_METRICS:
            for label, report in (("candidate", candidate), ("baseline", baseline)):
                if report.get(metric) is None:
                    reasons.append(f"{label} is missing required metric {metric!r}")

    for metric, lower_is_better in METRIC_DIRECTIONS.items():
        if candidate.get(metric) is None or baseline.get(metric) is None:
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
