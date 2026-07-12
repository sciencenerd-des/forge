"""Deterministic, task-aware memory retrieval.

The ranking follows the plan's research-grounded contract: similarity,
importance, confidence, and exponential recency are explicit terms; the
context budget is split into semantic, procedural, and episodic lanes. Every
database-specific tier falls back to a SQLite-safe Python implementation.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import and_, case, false, func, literal_column, or_, select

W_SIMILARITY = 0.50
W_IMPORTANCE = 0.20
W_CONFIDENCE = 0.15
W_RECENCY = 0.15
DEFAULT_RECENCY_HALFLIFE_HOURS = 72.0
DEFAULT_LANE_BUDGETS = {"semantic": 3000, "procedural": 1500, "episodic": 1500}
SEMANTIC_TYPES = {"decision", "constraint", "next_action", "bug", "blocker"}
PROCEDURAL_TYPES = {"lesson", "learning_distill"}

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "be", "build", "by", "for", "from",
    "goal", "in", "into", "is", "it", "make", "of", "on", "or", "task",
    "that", "the", "this", "to", "with", "work", "none",
}


def _terms(task_text: str) -> list[str]:
    out = []
    for token in re.findall(r"[A-Za-z0-9_./-]+", (task_text or "").lower()):
        token = token.strip("./-")
        if len(token) >= 3 and token not in _STOP_WORDS:
            out.append(token)
    return out


def memory_class(memory_type: str | None) -> str:
    """Map a memory type into the CoALA lanes used by the context budget."""
    value = (memory_type or "").lower()
    if value in SEMANTIC_TYPES:
        return "semantic"
    if value in PROCEDURAL_TYPES:
        return "procedural"
    return "episodic"


def _recency_halflife_hours() -> float:
    try:
        return max(0.0, float(os.getenv(
            "FORGE_MEMORY_RECENCY_HALFLIFE_HOURS",
            str(DEFAULT_RECENCY_HALFLIFE_HOURS),
        )))
    except (TypeError, ValueError):
        return DEFAULT_RECENCY_HALFLIFE_HOURS


def recency_score(created_at: datetime | None, memory_type: str | None,
                  now: datetime | None = None) -> float:
    """Exponential decay; constraints are authority-bearing and never stale."""
    if (memory_type or "").lower() == "constraint":
        return 1.0
    if created_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)
    half_life = _recency_halflife_hours()
    if half_life == 0:
        return 1.0 if age_hours == 0 else 0.0
    return math.exp(-age_hours / half_life)


def _scope_query(db, model, project_id: str, goal_task_ids: Sequence[str]):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    superseded_ids = select(model.supersedes_id).where(model.supersedes_id.isnot(None))
    memory_scope = false()
    if goal_task_ids:
        memory_scope = model.task_id.in_(list(goal_task_ids))
    memory_scope = or_(
        memory_scope,
        and_(model.task_id.is_(None), model.memory_type == "constraint"),
    )
    return db.query(model).filter(
        model.project_id == project_id,
        memory_scope,
        model.status == "active",
        or_(model.expires_at.is_(None), model.expires_at > now),
        model.id.notin_(superseded_ids),
    )


def _apply_char_budget(items: list[Any], char_budget: int) -> list[Any]:
    """Legacy single-lane helper retained for callers and compatibility tests."""
    if char_budget <= 0:
        return items
    kept: list[Any] = []
    used = 0
    for item in items:
        cost = len(item.content or "")
        if kept and used + cost > char_budget:
            continue
        kept.append(item)
        used += cost
    return kept


def _lane_budget(lane: str) -> int:
    try:
        return max(0, int(os.getenv(
            f"FORGE_MEMORY_{lane.upper()}_BUDGET",
            str(DEFAULT_LANE_BUDGETS[lane]),
        )))
    except (TypeError, ValueError):
        return DEFAULT_LANE_BUDGETS[lane]


def _apply_lane_budgets(items: list[Any], char_budget: int) -> list[Any]:
    """Apply lane budgets, then spend unused lane capacity as spillover."""
    if not items or char_budget <= 0:
        return items
    remaining = char_budget
    lane_used = {lane: 0 for lane in DEFAULT_LANE_BUDGETS}
    selected: list[Any] = []
    deferred: list[Any] = []
    for item in items:
        cost = len(item.content or "")
        lane = memory_class(getattr(item, "memory_type", None))
        if cost <= remaining and lane_used[lane] + cost <= _lane_budget(lane):
            selected.append(item)
            lane_used[lane] += cost
            remaining -= cost
        else:
            deferred.append(item)
    # A lane that had no evidence should not strand its budget.
    for item in deferred:
        cost = len(item.content or "")
        if cost <= remaining:
            selected.append(item)
            remaining -= cost
    return selected or items[:1]


def _expand_link_hits(db, items: list[Any], char_budget: int) -> list[Any]:
    if os.getenv("FORGE_MEMORY_LINK_EXPANSION", "1").lower() not in {"1", "true", "yes", "on"}:
        return items
    try:
        from .memory_links import expand_hits
        return expand_hits(db, items, char_budget)
    except Exception:
        return items


def select_memories(
    db,
    project_id: str,
    goal_task_ids: Sequence[str],
    task_text: str = "",
    *,
    limit: int = 20,
    char_budget: int = 6000,
    embed_query: Optional[Callable[[str], Optional[list[float]]]] = None,
) -> list[Any]:
    """Return scope-filtered memories ranked and lane-budgeted for a task."""
    from ..models import HermesMemoryItem as M

    base = _scope_query(db, M, project_id, goal_task_ids)
    dialect = ""
    try:
        dialect = db.bind.dialect.name
    except Exception:
        pass

    if dialect == "postgresql" and embed_query and task_text.strip():
        try:
            query_emb = embed_query(task_text)
            if query_emb:
                similarity = func.coalesce(1 - M.embedding.op("<=>")(query_emb), 0.0)
                age_hours = func.extract("epoch", func.now() - M.created_at) / 3600.0
                half_life = _recency_halflife_hours()
                if half_life == 0:
                    recency = case((M.memory_type == "constraint", 1.0), else_=0.0)
                else:
                    recency = case(
                        (M.memory_type == "constraint", 1.0),
                        else_=func.exp(-age_hours / half_life),
                    )
                score = (
                    W_SIMILARITY * similarity
                    + W_IMPORTANCE * (M.importance / 5.0)
                    + W_CONFIDENCE * M.confidence
                    + W_RECENCY * recency
                )
                items = base.order_by(score.desc(), M.created_at.desc()).limit(limit).all()
                if items:
                    return _apply_lane_budgets(_expand_link_hits(db, items, char_budget), char_budget)
        except Exception:
            db.rollback()

    if dialect == "postgresql" and task_text.strip() and _terms(task_text):
        try:
            tsquery = func.plainto_tsquery(literal_column("'english'"), task_text)
            tsvector = func.to_tsvector(literal_column("'english'"), M.content)
            rank = func.coalesce(func.ts_rank(tsvector, tsquery), 0.0)
            items = base.order_by(
                (rank + W_IMPORTANCE * (M.importance / 5.0)).desc(),
                M.created_at.desc(),
            ).limit(limit).all()
            if items:
                return _apply_lane_budgets(_expand_link_hits(db, items, char_budget), char_budget)
        except Exception:
            db.rollback()

    items = base.order_by(M.importance.desc(), M.created_at.desc()).limit(limit).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    items.sort(key=lambda item: (
        W_IMPORTANCE * (float(item.importance or 0) / 5.0)
        + W_CONFIDENCE * float(item.confidence or 0)
        + W_RECENCY * recency_score(item.created_at, item.memory_type, now),
        item.created_at or datetime.min,
    ), reverse=True)
    return _apply_lane_budgets(_expand_link_hits(db, items, char_budget), char_budget)
