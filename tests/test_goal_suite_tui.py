from __future__ import annotations

import os

import pytest

from evals.goal_suite import GOALS
from evals.orchestrator import build_provider_environment


@pytest.fixture
def _restore_environ():
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_provider_environment_is_explicit_and_does_not_mutate_parent(_restore_environ):
    os.environ["PGE_EVALUATOR_MODEL"] = "parent-model"
    before = dict(os.environ)

    child = build_provider_environment(
        model="google/gemma-4-12b-qat",
        base_url="http://127.0.0.1:1234/v1/",
        max_turns=24,
    )

    assert os.environ == before
    assert child["LLM_MODEL"] == "google/gemma-4-12b-qat"
    assert child["FORGE_LLM_BASE_URL"] == "http://127.0.0.1:1234/v1"
    assert child["FORGE_LLM_DIALECT"] == "openai"
    assert child["FORGE_LLM_REASONING_EFFORT"] == "none"
    assert child["FORGE_LLM_MAX_RETRIES"] == "0"
    assert child["LLM_BASE_URL"] == "http://127.0.0.1:1234/v1"
    assert child["PGE_AUDITOR_MODELS"] == "lmstudio:google/gemma-4-12b-qat"
    assert child["PGE_AUDITOR_CODEX"] == "0"
    assert child["PGE_STEWARD_BASE_URL"] == "http://127.0.0.1:1234/v1"
    assert child["PGE_MAX_TURNS"] == "24"
    assert child["PGE_EVALUATOR_MODEL"] == "google/gemma-4-12b-qat"


@pytest.mark.parametrize("module_name", ["evals.goal_suite_tui", "evals.runner"])
def test_cli_adapters_delegate_to_same_production_entrypoint(
    module_name, monkeypatch, tmp_path
):
    module = __import__(module_name, fromlist=["run_suite"])
    captured = {}
    expected = {"schema_version": "2.0", "goals": [], "completed": 0, "total": 0}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(module, "run_production_suite", fake_run)
    output = tmp_path / f"{module_name.rsplit('.', 1)[-1]}.json"
    report = module.run_suite(
        model="google/gemma-4-12b-qat",
        base_url="http://127.0.0.1:1234/v1",
        timeout=10,
        max_turns=3,
        output=output,
        only=1,
    )

    assert report is expected
    assert captured["model"] == "google/gemma-4-12b-qat"
    assert captured["base_url"] == "http://127.0.0.1:1234/v1"
    assert captured["only"] == 1
    assert captured["output"] == output
    assert captured["child_env"]["PGE_MAX_TURNS"] == "3"
    assert "configure" not in captured
    if module_name.endswith("goal_suite_tui"):
        assert captured["goals"] is GOALS
        assert "verify_override" not in captured
    else:
        assert callable(captured["verify_override"])


def test_tui_adapter_does_not_expose_lifecycle_callbacks():
    import evals.goal_suite_tui as tui

    for name in (
        "SessionLocal",
        "MemoryService",
        "load_run_state",
        "process_is_alive",
        "terminate_run",
        "orchestrate_suite",
    ):
        assert not hasattr(tui, name)
