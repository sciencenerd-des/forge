"""Phase 1: the versioned evidence contract.

These tests pin the rules that make runs comparable evidence: legacy quarantine,
required metadata, null-metric rejection, comparability fingerprints, atomic
checkpoint writes, and the legal lifecycle-state matrix.
"""

from __future__ import annotations

import json

import pytest

from evals import gate
from evals.contracts import (
    SCHEMA_VERSION,
    EnvironmentFingerprint,
    GoalResultV2,
    SuiteResultV2,
    Verdict,
    comparability_reasons,
    is_legal_state_pair,
    is_versioned,
    load_result_readonly,
    state_pair_reason,
    write_result_atomic,
)


# --------------------------------------------------------------------------- #
# Schema construction
# --------------------------------------------------------------------------- #
def test_schema_requires_run_and_goal_identity():
    goal = GoalResultV2(goal_id="lru", run_id="r1", project_id="p1",
                        launcher_status="completed", goal_status="completed",
                        outcome=Verdict.VERIFIED.value)
    assert goal.outcome == "verified"
    assert goal.turns_used is None  # unmeasured stays null, not zero


def test_unmeasured_metrics_are_null_not_zero():
    goal = GoalResultV2(goal_id="x", measurement_unavailable_reason="no ledger")
    dumped = goal.model_dump()
    for metric in ("turns_used", "cycles_to_done", "distance_auc", "token_cost"):
        assert dumped[metric] is None


def test_recompute_aggregates_counts_only_verified():
    suite = SuiteResultV2(goals=[
        GoalResultV2(goal_id="a", outcome=Verdict.VERIFIED.value),
        GoalResultV2(goal_id="b", outcome=Verdict.COMPLETE_UNVERIFIED.value, false_completion=True),
        GoalResultV2(goal_id="c", outcome=Verdict.TIMEOUT.value),
    ])
    suite.recompute_aggregates()
    assert suite.total == 3
    assert suite.completed == 1
    assert suite.verified_completion_rate == pytest.approx(1 / 3)
    assert suite.false_completion_count == 1
    assert suite.summary["timeout"] == 1


# --------------------------------------------------------------------------- #
# Legal lifecycle-state matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("launcher,goal,legal", [
    ("completed", "completed", True),
    ("completed", "active", False),      # false-green corruption
    ("completed", "blocked", False),
    ("blocked", "active", True),         # resumable, legitimate
    ("timeout", "active", True),
    ("stopped", "completed", True),      # ended after the goal was satisfied
    ("failed", "active", True),
])
def test_legal_state_pairs(launcher, goal, legal):
    assert is_legal_state_pair(launcher, goal) is legal
    assert (state_pair_reason(launcher, goal) is None) is legal


# --------------------------------------------------------------------------- #
# Comparability fingerprint
# --------------------------------------------------------------------------- #
def _v2(**env_overrides):
    env = {
        "schema_version": SCHEMA_VERSION, "suite_hash": "s1", "contract_hash": "c1",
        "model": "m", "provider": "p", "sandbox_image_digest": "d1",
        "max_turns": 24, "reasoning_effort": "none",
        "llm_request_timeout_s": 300.0, "llm_max_retries": 0,
        "replicate_count": 3,
    }
    env.update(env_overrides)
    return {"schema_version": SCHEMA_VERSION, "environment": env,
            "goals": [{"goal_id": "g", "outcome": "verified"}],
            "verified_completion_rate": 1.0, "mean_cycles_to_done": 5.0,
            "mean_distance_auc": 1.0, "token_cost": 100, "false_completion_count": 0}


def test_matching_v2_reports_are_comparable():
    assert comparability_reasons(_v2(), _v2()) == []


def test_mismatched_image_digest_is_incomparable():
    reasons = comparability_reasons(_v2(), _v2(sandbox_image_digest="d2"))
    assert any("sandbox_image_digest" in r for r in reasons)


def test_mismatched_reasoning_profile_is_incomparable():
    reasons = comparability_reasons(_v2(), _v2(reasoning_effort="low"))
    assert any("reasoning_effort" in r for r in reasons)


def test_legacy_dicts_skip_comparability():
    legacy = {"verified_completion_rate": 0.8, "goals": []}
    assert not is_versioned(legacy)
    assert comparability_reasons(legacy, _v2()) == []


# --------------------------------------------------------------------------- #
# Gate integration: v2 rigor
# --------------------------------------------------------------------------- #
def test_gate_passes_identity_v2():
    report = _v2()
    result = gate.evaluate_gate(dict(report), dict(report))
    assert result.passed, result.reasons


def test_gate_rejects_null_required_metric_in_v2():
    candidate = _v2()
    candidate["mean_cycles_to_done"] = None
    result = gate.evaluate_gate(candidate, _v2())
    assert not result.passed
    assert any("mean_cycles_to_done" in r for r in result.reasons)


def test_gate_refuses_incomparable_v2_before_metrics():
    result = gate.evaluate_gate(_v2(), _v2(model="other-model"))
    assert not result.passed
    assert any("incomparable model" in r for r in result.reasons)


def test_gate_reports_harness_failures_distinctly():
    candidate = _v2()
    candidate["goals"] = [{"goal_id": "g", "outcome": "harness_error"}]
    result = gate.evaluate_gate(candidate, _v2())
    assert not result.passed
    assert any("harness failures" in r for r in result.reasons)


# --------------------------------------------------------------------------- #
# Legacy quarantine + atomic writes
# --------------------------------------------------------------------------- #
def test_load_legacy_is_labeled_and_file_untouched(tmp_path):
    legacy = {"verified_completion_rate": 0.8, "goals": [{"goal_id": "g", "outcome": "verified"}]}
    path = tmp_path / "legacy.json"
    original = json.dumps(legacy)
    path.write_text(original)
    loaded = load_result_readonly(path)
    assert loaded["comparability"] == "legacy_uncomparable"
    assert path.read_text() == original  # never rewritten


def test_load_v2_is_not_labeled_legacy(tmp_path):
    path = tmp_path / "v2.json"
    write_result_atomic(path, _v2())
    loaded = load_result_readonly(path)
    assert "comparability" not in loaded


def test_truncated_json_raises_cleanly(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"goals": [')  # truncated
    with pytest.raises(json.JSONDecodeError):
        load_result_readonly(path)


def test_sample_v2_identity_comparison_passes():
    from pathlib import Path

    sample = Path(__file__).resolve().parent.parent / "evals" / "results" / "sample-v2.json"
    data = json.loads(sample.read_text())
    assert gate.evaluate_gate(data, data).passed


def test_self_improvement_gate_requires_min_replicates():
    one = _v2(replicate_count=1)
    # Standard gate passes identity; the self-improvement gate does not (1 < 3).
    assert gate.evaluate_gate(one, one).passed
    strict = gate.evaluate_self_improvement_gate(one, one, min_replicates=3)
    assert not strict.passed
    assert any("replicate" in r for r in strict.reasons)
    # Three replicates satisfies the paired-trial requirement.
    three = _v2(replicate_count=3)
    assert gate.evaluate_self_improvement_gate(three, three, min_replicates=3).passed


def test_atomic_write_leaves_valid_file_and_no_tmp(tmp_path):
    path = tmp_path / "sub" / "report.json"
    suite = SuiteResultV2(environment=EnvironmentFingerprint(model="m"))
    suite.goals.append(GoalResultV2(goal_id="a", outcome=Verdict.VERIFIED.value))
    suite.recompute_aggregates()
    write_result_atomic(path, suite)
    reloaded = json.loads(path.read_text())
    assert reloaded["schema_version"] == SCHEMA_VERSION
    assert reloaded["completed"] == 1
    # No leftover temp files from the replace.
    assert list(path.parent.glob("*.tmp")) == []
