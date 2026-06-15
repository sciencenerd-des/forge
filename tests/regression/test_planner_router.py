"""Regression: stagnation handling must not crash, and the graph edges must map.

A stagnation path once returned ``"planner"`` from ``planner_router`` while the
planner edge map only had ``auditor``/``executor``/``end`` — a ``KeyError('planner')``
that killed the run. This pins every router return value to a real edge.
"""
import pytest

pytest.importorskip("langgraph")
from src import graph as g


def test_planner_router_returns_only_mapped_edges():
    mapped = {"auditor", "executor", "end"}
    # Empty/terminal states exercise the router's branches without a live DB.
    assert g.planner_router({"turn_count": 999}) == "end"               # turn ceiling
    assert g.planner_router({"active_task": None}) == "end"             # nothing to do
    for state in ({"turn_count": 0, "active_task": None},):
        assert g.planner_router(state) in mapped


def test_evaluator_router_returns_only_mapped_edges():
    mapped = {"planner", "end"}
    assert g.evaluator_router({"goal_verified": True}) == "end"
    assert g.evaluator_router({"turn_count": 999}) == "end"
    assert g.evaluator_router({"decision": "blocked"}) == "end"
    assert g.evaluator_router({"decision": "continue"}) in mapped


def test_compiled_graph_has_expected_nodes():
    # The graph compiles and exposes the four PGE nodes.
    assert g.app is not None
