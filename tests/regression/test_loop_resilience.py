"""Regression: a single batch exception must NOT kill the detached loop.

Root cause of "the loop never persists": ``run_pge``'s batch loop caught only
*transient* errors and re-raised everything else, so any node-level bug (KeyError,
parse error, ...) crashed the whole process. Durable Postgres state means the next
batch resumes safely, so one bad batch must only cost a retry, not the run. Only a
SUSTAINED failure streak (the budget) ends the run — as ``blocked``, not a crash.
"""
import types

import pytest

pytest.importorskip("langgraph")  # run_pge imports the compiled graph
import run_pge  # noqa: E402


class _Goal:
    id = "g-1"; title = "T"; description = "d"; success_criteria = []; priority = 1
    def __init__(self): self.status = "active"


def _patch(monkeypatch, invoke, goal):
    n = {"i": 0}
    def load(_pid):
        n["i"] += 1
        return goal, ("g-1", "T", [("t1", "active", "x")], n["i"])  # changing fingerprint
    monkeypatch.setattr(run_pge, "_load_progress", load)
    monkeypatch.setattr(run_pge, "update_run", lambda *a, **k: True)
    monkeypatch.setattr(run_pge, "_initial_state", lambda pid, g=None: {"project_id": pid})
    ms = types.SimpleNamespace(sync_sqlite_to_postgres=lambda: "ok", record_event=lambda **k: None)
    monkeypatch.setattr(run_pge, "MemoryService", lambda db=None: ms)
    monkeypatch.setattr(run_pge, "SessionLocal", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(run_pge.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(run_pge, "pge_graph", types.SimpleNamespace(invoke=invoke))


def test_survives_node_and_transient_failures_then_completes(monkeypatch):
    goal = _Goal()
    calls = {"n": 0}
    def invoke(state, config=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("planner")            # non-transient node bug
        if calls["n"] == 2:
            raise TimeoutError("read timed out")  # transient
        goal.status = "completed"
        return {"decision": "complete", "turn_count": 1, "active_task": None}
    _patch(monkeypatch, invoke, goal)
    run_pge.run_pge("test-project")              # must NOT raise
    assert calls["n"] == 3, "loop should ride over both failures to completion"


def test_sustained_failures_end_as_blocked_not_unhandled_crash(monkeypatch):
    goal = _Goal()
    def invoke(state, config=None):
        raise KeyError("planner")                # never recovers
    _patch(monkeypatch, invoke, goal)
    monkeypatch.setenv("PGE_MAX_CONSECUTIVE_FAILURES", "3")
    with pytest.raises(RuntimeError, match="failure budget exhausted"):
        run_pge.run_pge("test-project")          # graceful giveup, not raw KeyError
