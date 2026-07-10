"""Pins Phase 6 of specs/convergent-autonomous-harness.html: eval-gated self-improvement."""
import json
from pathlib import Path

from evals.gate import evaluate_gate, validate_whitelist


def _baseline():
    return {
        "verified_completion_rate": 0.8,
        "mean_cycles_to_done": 6.0,
        "mean_distance_auc": 1.5,
        "false_completion_count": 0,
        "token_cost": 50000,
        "goals": [{"goal_id": "goal-1", "outcome": "verified"}],
    }


def test_identical_candidate_passes():
    baseline = _baseline()
    result = evaluate_gate(dict(baseline), baseline)
    assert result.passed


def test_any_false_completion_hard_fails_regardless_of_other_metrics():
    baseline = _baseline()
    candidate = dict(baseline)
    candidate["verified_completion_rate"] = 0.95  # better everywhere else
    candidate["false_completion_count"] = 1
    result = evaluate_gate(candidate, baseline)
    assert not result.passed
    assert any("false completion" in r for r in result.reasons)


def test_verified_rate_drop_is_a_regression():
    baseline = _baseline()
    candidate = dict(baseline)
    candidate["verified_completion_rate"] = 0.5
    result = evaluate_gate(candidate, baseline)
    assert not result.passed


def test_cycles_improvement_with_no_other_change_passes():
    baseline = _baseline()
    candidate = dict(baseline)
    candidate["mean_cycles_to_done"] = 4.0  # fewer cycles is better
    result = evaluate_gate(candidate, baseline)
    assert result.passed


def test_missing_metric_is_skipped_not_a_crash():
    baseline = {
        "verified_completion_rate": 0.8,
        "false_completion_count": 0,
        "goals": [{"goal_id": "goal-1", "outcome": "verified"}],
    }
    candidate = {
        "verified_completion_rate": 0.9,
        "false_completion_count": 0,
        "goals": [{"goal_id": "goal-1", "outcome": "verified"}],
    }
    result = evaluate_gate(candidate, baseline)
    assert result.passed


def test_incomplete_or_missing_goal_evidence_fails_closed():
    baseline = _baseline()
    candidate = dict(baseline)
    candidate["goals"] = [{"goal_id": "goal-1", "outcome": "skipped"}]
    result = evaluate_gate(candidate, baseline)
    assert not result.passed
    assert any("incomplete benchmark" in reason for reason in result.reasons)

    no_evidence = dict(baseline)
    no_evidence["goals"] = []
    result = evaluate_gate(no_evidence, baseline)
    assert not result.passed
    assert any("no per-goal" in reason for reason in result.reasons)


def test_whitelist_accepts_only_known_config_knobs():
    ok, rejected = validate_whitelist({"controller_epsilon": 0.3})
    assert ok
    assert rejected == ()


def test_whitelist_rejects_engine_code_changes():
    ok, rejected = validate_whitelist({"engine/src/graph.py": "..."})
    assert not ok
    assert "engine/src/graph.py" in rejected


def test_sample_result_self_comparison_via_cli(tmp_path: Path):
    sample = Path(__file__).resolve().parent.parent.parent / "evals" / "results" / "sample.json"
    data = json.loads(sample.read_text())
    result = evaluate_gate(data, data)
    assert result.passed
