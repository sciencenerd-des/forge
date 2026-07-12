"""Deterministic memory associations and one-hop retrieval expansion.

Links are encoded in the existing ``tags`` array to keep SQLite fixtures and
existing deployments migration-free. Link generation never calls an LLM and
is best-effort at the write boundary.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_

LINK_PREFIX = "link:"
MAX_LINKS = 8
PROTECTED_EVOLUTION_TYPES = {"constraint", "decision"}


def _tags(row: Any) -> list[str]:
    return list(row.tags or [])


def _link_ids(row: Any) -> list[str]:
    return [tag[len(LINK_PREFIX):] for tag in _tags(row) if tag.startswith(LINK_PREFIX)]


def _shared_tags(left: Any, right: Any) -> set[str]:
    excluded = ("fp:", LINK_PREFIX)
    return {
        tag for tag in set(_tags(left)) & set(_tags(right))
        if not tag.startswith(excluded)
    }


def _cosine(left: Any, right: Any) -> float | None:
    try:
        a, b = list(left), list(right)
        if not a or len(a) != len(b):
            return None
        norm_a = math.sqrt(sum(value * value for value in a))
        norm_b = math.sqrt(sum(value * value for value in b))
        if not norm_a or not norm_b:
            return None
        return sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _append_link(row: Any, target_id: str) -> bool:
    tags = _tags(row)
    marker = f"{LINK_PREFIX}{target_id}"
    if marker in tags:
        return False
    links = [tag for tag in tags if tag.startswith(LINK_PREFIX)]
    if len(links) >= MAX_LINKS:
        return False
    row.tags = [*tags, marker]
    return True


def generate_links(db, item: Any, k: int = 3) -> tuple[str, ...]:
    """Link a new item to up to ``k`` deterministic same-project neighbors."""
    if not getattr(item, "id", None) or k <= 0:
        return ()
    from ..models import HermesMemoryItem

    candidates = db.query(HermesMemoryItem).filter(
        HermesMemoryItem.project_id == item.project_id,
        HermesMemoryItem.id != item.id,
        HermesMemoryItem.status == "active",
        or_(HermesMemoryItem.supersedes_id.is_(None),
            HermesMemoryItem.supersedes_id != item.id),
    ).order_by(HermesMemoryItem.created_at.desc()).limit(64).all()

    ranked: list[tuple[tuple[int, int, float, str], Any]] = []
    for candidate in candidates:
        shared = _shared_tags(item, candidate)
        same_file = bool(item.file_path and candidate.file_path == item.file_path)
        # pgvector returns numpy arrays on Postgres; truthiness on an array
        # raises ValueError (observed live: every link silently swallowed by
        # the best-effort wrapper). Identity-check None instead.
        similarity = (
            _cosine(item.embedding, candidate.embedding)
            if item.embedding is not None and candidate.embedding is not None
            else None
        )
        if not shared and not same_file and (similarity is None or similarity < 0.75):
            continue
        ranked.append((
            (1 if shared else 0, 1 if same_file else 0, similarity or 0.0, candidate.id),
            candidate,
        ))
    ranked.sort(key=lambda pair: pair[0], reverse=True)

    linked: list[str] = []
    for _, candidate in ranked:
        if len(linked) >= k or len(_link_ids(item)) >= MAX_LINKS:
            break
        if _append_link(item, candidate.id):
            _append_link(candidate, item.id)
            linked.append(candidate.id)
    if linked:
        db.commit()
        db.refresh(item)
    return tuple(linked)


def expand_hits(db, items: list[Any], budget: int) -> list[Any]:
    """Append active one-hop neighbors while respecting the remaining budget."""
    if not items or budget <= 0:
        return items
    from ..models import HermesMemoryItem

    selected_ids = {item.id for item in items}
    linked_ids: list[str] = []
    for item in items:
        for link_id in _link_ids(item):
            if link_id not in selected_ids and link_id not in linked_ids:
                linked_ids.append(link_id)
    if not linked_ids:
        return items
    candidates = db.query(HermesMemoryItem).filter(
        HermesMemoryItem.id.in_(linked_ids),
        HermesMemoryItem.status == "active",
    ).all()
    by_id = {candidate.id: candidate for candidate in candidates}
    used = sum(len(item.content or "") for item in items)
    expanded = list(items)
    for link_id in linked_ids:
        candidate = by_id.get(link_id)
        if candidate is None:
            continue
        cost = len(candidate.content or "")
        if used + cost > budget:
            continue
        expanded.append(candidate)
        selected_ids.add(candidate.id)
        used += cost
    return expanded


def evolve_memories(db, project_id: str, batch: int = 8) -> int:
    """Append a dated superseded-context note to linked older lessons/mistakes."""
    from ..models import HermesMemoryItem

    rows = db.query(HermesMemoryItem).filter(
        HermesMemoryItem.project_id == project_id,
        HermesMemoryItem.memory_type.in_(("mistake", "lesson")),
        HermesMemoryItem.status == "active",
    ).order_by(HermesMemoryItem.created_at.desc(), HermesMemoryItem.id.desc()).limit(max(1, batch)).all()
    changed = 0
    today = datetime.now(timezone.utc).date().isoformat()
    by_id = {
        row.id: row for row in db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == project_id,
            HermesMemoryItem.status == "active",
        ).all()
    }
    for newer in rows:
        if newer.memory_type in PROTECTED_EVOLUTION_TYPES:
            continue
        summary = " ".join((newer.content or "").split())[:160]
        for older_id in _link_ids(newer):
            older = by_id.get(older_id)
            if older is None or older.id == newer.id or older.memory_type != newer.memory_type:
                continue
            if older.created_at and newer.created_at and older.created_at >= newer.created_at:
                continue
            suffix = f"[update {today}] superseded context: {summary}"
            if suffix in (older.content or ""):
                continue
            older.content = f"{older.content.rstrip()}\n{suffix}"
            changed += 1
    if changed:
        db.commit()
    return changed
