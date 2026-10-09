"""Phase 6: Pi tool benchmark — protocol, quantiles, scenarios, aborts.

Uses pinned Pi 0.80.2 event fixtures so the analysis is exercised without a live
Pi executable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pi_tool_bench as bench  # noqa: E402


# --------------------------------------------------------------------------- #
# Quantiles
# --------------------------------------------------------------------------- #
def test_nearest_rank_p95_of_two_is_the_max_not_the_min():
    # The old int(n*0.95)-1 formula returned index 0 (the minimum) for n=2,
    # producing a "p95" below the median. Nearest-rank returns the max.
    assert bench.percentile_nearest_rank([0.010, 0.020], 0.95) == 0.020


def test_nearest_rank_single_sample():
    assert bench.percentile_nearest_rank([0.005], 0.95) == 0.005


def test_nearest_rank_empty_is_none():
    assert bench.percentile_nearest_rank([], 0.95) is None


def test_summary_enforces_median_le_p95_le_max():
    summary = bench.summarize({"read": [0.001, 0.002, 0.003, 0.050]})
    row = summary["read"]
    assert row["median_ms"] <= row["p95_ms"] <= row["max_ms"]


# --------------------------------------------------------------------------- #
# Event reduction + pairing
# --------------------------------------------------------------------------- #
def _clean_read_events():
    return [
        {"type": "agent_start", "_ts": 100.0},
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read", "_ts": 100.1},
        {"type": "tool_execution_end", "toolCallId": "t1", "_ts": 100.105},
        {"type": "tool_execution_start", "toolCallId": "t2", "toolName": "read", "_ts": 100.2},
        {"type": "tool_execution_end", "toolCallId": "t2", "_ts": 100.203},
        {"type": "agent_end", "text": "done", "_ts": 100.5},
    ]


def test_reduce_pairs_tools_by_id_and_measures_turn():
    run = bench.reduce_events(_clean_read_events())
    assert len(run.tool_samples["read"]) == 2
    assert run.tool_samples["read"][0] == pytest.approx(0.005, abs=1e-6)
    assert run.turns == pytest.approx([0.5])
    assert run.clean_end and run.final_answer == "done"
    assert run.unpaired_tool_ids == []


def test_reduce_flags_unpaired_tool_start():
    events = [
        {"type": "agent_start", "_ts": 0.0},
        {"type": "tool_execution_start", "toolCallId": "x", "toolName": "bash", "_ts": 0.1},
        {"type": "agent_end", "text": "done", "_ts": 0.4},
    ]
    run = bench.reduce_events(events)
    assert run.unpaired_tool_ids == ["x"]
    assert "bash" not in run.tool_samples  # no spurious latency from an unpaired start


# --------------------------------------------------------------------------- #
# Scenario validation
# --------------------------------------------------------------------------- #
def test_clean_scenario_is_valid():
    run = bench.reduce_events(_clean_read_events())
    # read expects >= 5 calls; this fixture only has 2, so lower the bar to test
    # the happy path of the other predicates.
    result = bench.validate_scenario("read", run, {"expected_answer": "done", "min_tool_calls": 2})
    assert result.valid, result.reasons


def test_wrong_final_answer_is_invalid():
    events = _clean_read_events()
    events[-1] = {"type": "agent_end", "text": "not done", "_ts": 100.5}
    run = bench.reduce_events(events)
    result = bench.validate_scenario("read", run, {"expected_answer": "done", "min_tool_calls": 2})
    assert not result.valid
    assert any("final answer" in r for r in result.reasons)


def test_too_few_tool_calls_is_invalid():
    run = bench.reduce_events(_clean_read_events())
    result = bench.validate_scenario("read", run, {"expected_answer": "done", "min_tool_calls": 5})
    assert not result.valid
    assert any("tool calls" in r for r in result.reasons)


def test_aborted_scenario_is_invalid_and_excluded_from_latency():
    events = [
        {"type": "agent_start", "_ts": 0.0},
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read", "_ts": 0.1},
        {"type": "tool_execution_end", "toolCallId": "t1", "_ts": 0.11},
        {"type": "abort", "_ts": 5.0},
    ]
    run = bench.reduce_events(events)
    result = bench.validate_scenario("read", run, {"expected_answer": "done", "min_tool_calls": 1})
    assert run.aborted
    assert not result.valid
    assert "aborted" in result.reasons
    # No clean agent_end -> turn is not counted as a completed turn.
    assert run.turns == []


def test_missing_terminal_event_is_not_clean():
    events = [
        {"type": "agent_start", "_ts": 0.0},
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read", "_ts": 0.1},
        {"type": "tool_execution_end", "toolCallId": "t1", "_ts": 0.11},
    ]
    run = bench.reduce_events(events)
    assert run.clean_end is False
    result = bench.validate_scenario("read", run, {"expected_answer": "done", "min_tool_calls": 1})
    assert not result.valid
    assert any("agent_end" in r for r in result.reasons)
