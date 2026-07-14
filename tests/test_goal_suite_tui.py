from __future__ import annotations

import os

import pytest

from evals.goal_suite_tui import configure_suite_provider
from forge_config import ROLES, provider_for


@pytest.fixture
def _restore_environ():
    """configure_suite_provider mutates os.environ by design (process-local
    provider config inherited by the launcher child). Snapshot and restore the
    whole environment so this test cannot leak LLM_MODEL/PGE_*_MODEL into later
    tests (which previously broke test_llm_factory when run in the full suite)."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_suite_provider_overrides_role_specific_environment(_restore_environ):
    os.environ["PGE_EVALUATOR_MODEL"] = "wrong-model"
    os.environ["FORGE_PLANNER_BASE_URL"] = "http://wrong-host/v1"

    configure_suite_provider(
        model="google/gemma-4-12b-qat",
        base_url="http://127.0.0.1:1234/v1/",
    )

    assert os.environ["LLM_MODEL"] == "google/gemma-4-12b-qat"
    assert os.environ["FORGE_LLM_BASE_URL"] == "http://127.0.0.1:1234/v1"
    assert os.environ["FORGE_LLM_DIALECT"] == "openai"
    for role in ROLES:
        upper = role.upper()
        assert os.environ[f"PGE_{upper}_MODEL"] == "google/gemma-4-12b-qat"
        assert os.environ[f"FORGE_{upper}_BASE_URL"] == "http://127.0.0.1:1234/v1"
        assert provider_for(role)["model"] == "google/gemma-4-12b-qat"
        assert provider_for(role)["base_url"] == "http://127.0.0.1:1234/v1"
