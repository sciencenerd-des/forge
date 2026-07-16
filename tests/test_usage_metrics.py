from types import SimpleNamespace

import pytest

from forge_runtime.usage import record_response_usage, response_total_tokens
from run_pge import _append_distance_sample, _distance_from_state


def test_response_total_tokens_supports_openai_and_ollama_shapes():
    response = SimpleNamespace(usage=SimpleNamespace(total_tokens=37))
    assert response_total_tokens(response) == 37
    assert response_total_tokens({"usage": {"total_tokens": 12}}) == 12
    assert response_total_tokens({"prompt_eval_count": 4, "eval_count": 9}) == 13
    assert response_total_tokens({"choices": []}) is None


def test_record_response_usage_increments_current_manifest(monkeypatch):
    recorded = []
    monkeypatch.setenv("FORGE_PROJECT_ID", "p1")
    monkeypatch.setenv("FORGE_RUN_ID", "r1")
    monkeypatch.setattr(
        "pge_launcher.increment_run_metric",
        lambda project, run, metric, amount: recorded.append((project, run, metric, amount)),
    )

    assert record_response_usage({"usage": {"total_tokens": 23}}) == 23
    assert recorded == [("p1", "r1", "token_cost", 23)]


def test_distance_trace_and_auc_are_measured_from_contract_state():
    first = {"last_pass_ids": ["T1"], "last_eval": {"missing_items": ["T2"]}}
    second = {"decision": "complete", "last_pass_ids": ["T1", "T2"]}
    assert _distance_from_state(first) == pytest.approx(0.5)
    assert _distance_from_state(second) == 0.0

    trace, auc = _append_distance_sample({}, 0.5)
    assert trace == [0.5] and auc == 0.5
    trace, auc = _append_distance_sample({"distance_trace": trace, "distance_auc": auc}, 0.0)
    assert trace == [0.5, 0.0] and auc == pytest.approx(0.75)
