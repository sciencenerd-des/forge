"""Phase 1: recency decay, lane budgets, and deterministic ranking."""
from datetime import datetime, timedelta

from app import models as m
from app.services.memory_retrieval import (
    _apply_lane_budgets,
    memory_class,
    recency_score,
    select_memories,
)


def test_memory_classes_are_deterministic():
    assert memory_class("constraint") == "semantic"
    assert memory_class("lesson") == "procedural"
    assert memory_class("learning_fail") == "episodic"
    assert memory_class("unknown") == "episodic"


def test_recency_decay_and_constraint_exemption(monkeypatch):
    now = datetime(2026, 1, 1)
    monkeypatch.setenv("FORGE_MEMORY_RECENCY_HALFLIFE_HOURS", "72")
    fresh = recency_score(now, "mistake", now)
    old = recency_score(now - timedelta(hours=72), "mistake", now)
    assert fresh == 1.0
    assert 0 < old < fresh
    assert recency_score(now - timedelta(days=365), "constraint", now) == 1.0


def test_zero_half_life_is_safe(monkeypatch):
    monkeypatch.setenv("FORGE_MEMORY_RECENCY_HALFLIFE_HOURS", "0")
    now = datetime(2026, 1, 1)
    assert recency_score(now, "lesson", now) == 1.0
    assert recency_score(now - timedelta(seconds=1), "lesson", now) == 0.0


def test_lane_budget_spillover_keeps_multiple_classes(monkeypatch):
    class Item:
        def __init__(self, kind, content):
            self.memory_type = kind
            self.content = content

    monkeypatch.setenv("FORGE_MEMORY_SEMANTIC_BUDGET", "20")
    monkeypatch.setenv("FORGE_MEMORY_PROCEDURAL_BUDGET", "20")
    monkeypatch.setenv("FORGE_MEMORY_EPISODIC_BUDGET", "20")
    items = [Item("decision", "s" * 20), Item("lesson", "p" * 20), Item("mistake", "e" * 20)]
    assert [item.memory_type for item in _apply_lane_budgets(items, 60)] == [
        "decision", "lesson", "mistake"
    ]


def test_sqlite_selection_prefers_fresh_confident_items(sqlite_db):
    project = m.HermesProject(name="p", repo_path="/tmp/p")
    sqlite_db.add(project)
    sqlite_db.commit()
    goal = m.HermesGoal(project_id=project.id, title="memory scoring")
    sqlite_db.add(goal)
    sqlite_db.commit()
    task = m.HermesTask(project_id=project.id, goal_id=goal.id, title="score", status="active")
    sqlite_db.add(task)
    sqlite_db.commit()
    for kind, content, importance in (
        ("decision", "fresh decision", 3),
        ("lesson", "procedural lesson", 3),
        ("mistake", "episodic mistake", 3),
    ):
        sqlite_db.add(m.HermesMemoryItem(
            project_id=project.id, task_id=task.id, memory_type=kind,
            content=content, importance=importance, confidence=0.9, tags=[]))
    sqlite_db.commit()
    rows = select_memories(sqlite_db, project.id, [task.id], "score", char_budget=1000)
    assert {row.content for row in rows} == {
        "fresh decision", "procedural lesson", "episodic mistake"
    }
