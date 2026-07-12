"""Compounding memory: extract, dedupe, retrieve failure lessons.

Activates the ``Lesson`` dataclass already declared in
``forge_runtime/steering.py``: when the controller sees the *same* failure
fingerprint recur, an extraction pass distills observation + prevention.
Lessons are deduped on fingerprint (repeats bump confidence, never
duplicate), decayed when never retrieved-and-useful, and retrieved into the
steward's context pack under a hard token budget.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from forge_runtime.steering import Lesson

MIN_PREVENTION_LEN = 20
CONFIDENCE_PRUNE_THRESHOLD = 0.3
CONFIDENCE_BUMP = 0.05
CONFIDENCE_DECAY = 0.05


def fingerprint(failure_type: str, test_id: str, error_signature: str) -> str:
    payload = f"{failure_type}:{test_id}:{error_signature}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FailureEvent:
    __test__ = False  # not a pytest test case despite the name prefix

    task_id: str
    failure_type: str
    test_id: str
    error_signature: str


def should_extract(events: Sequence[FailureEvent], fp: str) -> bool:
    """Only extract on the second-plus occurrence of a fingerprint, to bound LLM cost."""
    count = sum(
        1
        for e in events
        if fingerprint(e.failure_type, e.test_id, e.error_signature) == fp
    )
    return count >= 2


def extract_lesson(
    event: FailureEvent,
    llm_extract: Callable[[FailureEvent], dict],
    evidence_ids: Sequence[str] = (),
) -> Lesson | None:
    """Distill a Lesson from a failure event via ``llm_extract`` (injected for testability).

    ``llm_extract`` must return {"observation": str, "prevention": str}.
    Rejects vacuous extractions (too short, or a restatement of the error).
    """
    result = llm_extract(event)
    prevention = (result or {}).get("prevention", "").strip()
    observation = (result or {}).get("observation", "").strip()
    if len(prevention) < MIN_PREVENTION_LEN:
        return None
    if prevention.lower() == event.error_signature.strip().lower():
        return None

    fp = fingerprint(event.failure_type, event.test_id, event.error_signature)
    return Lesson(
        fingerprint=fp,
        task_id=event.task_id,
        failure_type=event.failure_type,
        observation=observation,
        prevention=prevention,
        evidence_ids=tuple(evidence_ids),
        confidence=0.9,
    )


class LessonStore:
    """In-memory lesson store with upsert-on-fingerprint semantics.

    A real deployment backs this with the ``lessons`` table (migration 002);
    this class is the pure logic layer the service wraps.
    """

    def __init__(self) -> None:
        self._by_fingerprint: dict[str, Lesson] = {}

    def upsert(self, lesson: Lesson) -> Lesson:
        existing = self._by_fingerprint.get(lesson.fingerprint)
        if existing is None:
            self._by_fingerprint[lesson.fingerprint] = lesson
            return lesson
        bumped = replace(existing, confidence=min(1.0, existing.confidence + CONFIDENCE_BUMP))
        self._by_fingerprint[lesson.fingerprint] = bumped
        return bumped

    def mark_unmatched(self, fingerprints: Sequence[str]) -> None:
        """Decay confidence for lessons that were not matched this cycle; prune below threshold."""
        for fp in fingerprints:
            existing = self._by_fingerprint.get(fp)
            if existing is None:
                continue
            decayed = replace(existing, confidence=existing.confidence - CONFIDENCE_DECAY)
            if decayed.confidence < CONFIDENCE_PRUNE_THRESHOLD:
                del self._by_fingerprint[fp]
            else:
                self._by_fingerprint[fp] = decayed

    def all(self) -> tuple[Lesson, ...]:
        return tuple(self._by_fingerprint.values())

    def retrieve(
        self, failure_type_prefix: str, top_k: int = 3, max_chars: int = 800
    ) -> tuple[Lesson, ...]:
        """Top-k lessons matching a failure-class prefix, ranked by confidence, token-capped."""
        matches = sorted(
            (l for l in self._by_fingerprint.values() if l.failure_type.startswith(failure_type_prefix)),
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
