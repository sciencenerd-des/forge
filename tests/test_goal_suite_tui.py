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


def test_suite_timeout_terminates_the_detached_run(monkeypatch, tmp_path, _restore_environ):
    """The recorded July-13 P0: a suite timeout must stop the whole process
    group and finalize the manifest — never leave the orphan contending with
    the next goal."""
    import evals.goal_suite_tui as tui

    terminated = {}

    class _FakeProject:
        id = "proj-1"

    class _FakeService:
        def __init__(self, db):
            pass

        def create_project(self, name, repo_path):
            return _FakeProject()

        def create_goal(self, **kwargs):
            return None

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tui, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(tui, "MemoryService", _FakeService)
    monkeypatch.setattr(tui, "configure_suite_provider", lambda **kw: None)
    monkeypatch.setattr(tui, "load_run_state",
                        lambda: {"proj-1": {"status": "running", "pid": 4242}})
    monkeypatch.setattr(tui, "process_is_alive", lambda pid: True)
    monkeypatch.setattr(
        tui, "terminate_run",
        lambda project_id, run_id, reason, **kw: terminated.update(
            {"project_id": project_id, "run_id": run_id, "reason": reason, **kw}) or {},
    )
    monkeypatch.setattr(
        tui, "_verdict",
        lambda project, slug: {"slug": slug, "goal_status": "active",
                               "acceptance_verdict": "unverifiable", "accepted": False,
                               "acceptance_reason": "not finished"},
    )
    import pge_launcher
    monkeypatch.setattr(pge_launcher, "launch_pge",
                        lambda *a, **k: {"status": "success", "run_id": "run-9"})

    report = tui.run_suite(model="m", base_url="http://x/v1", timeout=0,
                           max_turns=1, output=tmp_path / "out.json", only=1)

    assert terminated["project_id"] == "proj-1"
    assert terminated["run_id"] == "run-9"
    assert terminated["status"] == "timeout"
    goal = report["goals"][0]
    # timeout + active goal is a legitimate resumable pair, NOT "inconsistent".
    assert goal["status"] == "timeout"
    assert goal["outcome"] == "blocked"
