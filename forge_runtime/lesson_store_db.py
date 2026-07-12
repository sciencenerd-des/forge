"""Durable lesson store backed by ``hermes_memory_items``.

``forge_runtime.lessons.LessonStore`` is the pure logic layer and lives only
as an in-process dict — every run relearned the same failures. This wrapper
gives the same interface durability using columns the memory table already
has: the fingerprint rides in ``tags`` as ``fp:<hex>``, confidence in the
``confidence`` column, and observation/prevention as JSON in ``content``.

Decay archives (``status="archived"``) instead of deleting — the pack filter
(``status == "active"``) drops archived lessons for free while the row stays
auditable, matching the repo invariant that durable evidence is never erased.

Every method is best-effort against a live session; callers on the loop path
must treat failures as "no lessons", never as a crash.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Sequence

from forge_runtime.lessons import (
    CONFIDENCE_BUMP,
    CONFIDENCE_DECAY,
    CONFIDENCE_PRUNE_THRESHOLD,
)
from forge_runtime.steering import Lesson

MEMORY_TYPE = "lesson"
FP_TAG_PREFIX = "fp:"


def _fp_tag(fingerprint: str) -> str:
    return f"{FP_TAG_PREFIX}{fingerprint}"


def _to_row_content(lesson: Lesson) -> str:
    return json.dumps({
        "observation": lesson.observation,
        "prevention": lesson.prevention,
        "failure_type": lesson.failure_type,
        "evidence_ids": list(lesson.evidence_ids),
    }, sort_keys=True)


def _from_row(row) -> Lesson | None:
    fingerprint = next(
        (t[len(FP_TAG_PREFIX):] for t in (row.tags or []) if t.startswith(FP_TAG_PREFIX)),
        None,
    )
    if not fingerprint:
        return None
    try:
        data = json.loads(row.content)
    except Exception:
        return None
    return Lesson(
        fingerprint=fingerprint,
        task_id=row.task_id or "",
        failure_type=data.get("failure_type", ""),
        observation=data.get("observation", ""),
        prevention=data.get("prevention", ""),
        evidence_ids=tuple(data.get("evidence_ids", [])),
        confidence=float(row.confidence or 0),
    )


class DbLessonStore:
    """``LessonStore``-shaped API persisting to ``hermes_memory_items``."""

    def __init__(self, db, project_id: str) -> None:
        self.db = db
        self.project_id = project_id

    # -- internals ---------------------------------------------------------

    def _row_for(self, fingerprint: str):
        from app.models import HermesMemoryItem
        candidates = self.db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == self.project_id,
            HermesMemoryItem.memory_type == MEMORY_TYPE,
        ).all()
        tag = _fp_tag(fingerprint)
        for row in candidates:
            if tag in (row.tags or []):
                return row
        return None

    def _active_rows(self):
        from app.models import HermesMemoryItem
        return self.db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == self.project_id,
            HermesMemoryItem.memory_type == MEMORY_TYPE,
            HermesMemoryItem.status == "active",
        ).all()

    # -- LessonStore interface ---------------------------------------------

    def upsert(self, lesson: Lesson) -> Lesson:
        from app.models import HermesMemoryItem
        row = self._row_for(lesson.fingerprint)
        if row is None:
            row = HermesMemoryItem(
                project_id=self.project_id,
                task_id=lesson.task_id or None,
                memory_type=MEMORY_TYPE,
                content=_to_row_content(lesson),
                confidence=lesson.confidence,
                importance=4,
                tags=[_fp_tag(lesson.fingerprint), MEMORY_TYPE],
                status="active",
            )
            self.db.add(row)
            self.db.commit()
            return lesson
        bumped = min(1.0, float(row.confidence or 0) + CONFIDENCE_BUMP)
        row.confidence = bumped
        row.status = "active"  # a recurring lesson resurrects an archived one
        self.db.commit()
        return replace(lesson, confidence=bumped)

    def mark_unmatched(self, fingerprints: Sequence[str]) -> None:
        """Decay listed lessons; archive (never delete) below the threshold."""
        wanted = {_fp_tag(fp) for fp in fingerprints}
        changed = False
        for row in self._active_rows():
            if not wanted.intersection(row.tags or []):
                continue
            decayed = float(row.confidence or 0) - CONFIDENCE_DECAY
            row.confidence = decayed
            if decayed < CONFIDENCE_PRUNE_THRESHOLD:
                row.status = "archived"
            changed = True
        if changed:
            self.db.commit()

    def all(self) -> tuple[Lesson, ...]:
        out = []
        for row in self._active_rows():
            lesson = _from_row(row)
            if lesson:
                out.append(lesson)
        return tuple(out)

    def retrieve(
        self, failure_type_prefix: str, top_k: int = 3, max_chars: int = 800
    ) -> tuple[Lesson, ...]:
        matches = sorted(
            (l for l in self.all() if l.failure_type.startswith(failure_type_prefix)),
            key=lambda l: l.confidence,
            reverse=True,
        )
        selected: list[Lesson] = []
        budget = max_chars
        for lesson in matches:
            if len(selected) >= top_k:
                break
            cost = len(lesson.observation) + len(lesson.prevention)
            if cost > budget:
                continue
            selected.append(lesson)
            budget -= cost
        return tuple(selected)
