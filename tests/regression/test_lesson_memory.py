"""Pins Phase 4 of specs/convergent-autonomous-harness.html: compounding memory."""
from forge_runtime.lessons import (
    FailureEvent,
    LessonStore,
    extract_lesson,
    fingerprint,
    should_extract,
)


def _event(n=1):
    return FailureEvent(
        task_id="task-1", failure_type="test_failure",
        test_id="t1", error_signature=f"AssertionError: expected {n}",
    )


def test_extraction_gated_until_second_occurrence():
    e = _event()
    fp = fingerprint(e.failure_type, e.test_id, e.error_signature)
    assert not should_extract([e], fp)
    assert should_extract([e, e], fp)


def test_vacuous_extraction_rejected_too_short():
    llm = lambda ev: {"observation": "x", "prevention": "too short"}
    assert extract_lesson(_event(), llm) is None


def test_vacuous_extraction_rejected_restates_error():
    llm = lambda ev: {"observation": "x", "prevention": ev.error_signature}
    assert extract_lesson(_event(), llm) is None


def test_valid_extraction_produces_lesson():
    llm = lambda ev: {
        "observation": "the fixture wasn't reset between runs",
        "prevention": "always call reset_fixture() before asserting state in this test class",
    }
    lesson = extract_lesson(_event(), llm, evidence_ids=("ev1",))
    assert lesson is not None
    assert lesson.evidence_ids == ("ev1",)
    assert lesson.confidence == 0.9


def test_duplicate_fingerprint_bumps_confidence_not_duplicates():
    store = LessonStore()
    llm = lambda ev: {
        "observation": "obs",
        "prevention": "a sufficiently long prevention string for the gate",
    }
    lesson = extract_lesson(_event(), llm)
    store.upsert(lesson)
    store.upsert(lesson)
    assert len(store.all()) == 1
    assert store.all()[0].confidence > 0.9


def test_retrieval_ranks_by_confidence_and_respects_prefix():
    store = LessonStore()
    l1 = extract_lesson(
        FailureEvent("t1", "test_failure", "a", "err a"),
        lambda ev: {"observation": "o1", "prevention": "prevention string one long enough"},
    )
    l2 = extract_lesson(
        FailureEvent("t1", "tool_error", "b", "err b"),
        lambda ev: {"observation": "o2", "prevention": "prevention string two long enough"},
    )
    store.upsert(l1)
    store.upsert(l2)
    store.upsert(l1)  # bump l1 above l2
    results = store.retrieve("test_failure")
    assert len(results) == 1
    assert results[0].fingerprint == l1.fingerprint


def test_retrieval_respects_token_budget():
    store = LessonStore()
    for i in range(5):
        lesson = extract_lesson(
            FailureEvent("t1", "test_failure", f"t{i}", f"err {i}"),
            lambda ev: {"observation": "o" * 100, "prevention": "p" * 100 + "x" * 20},
        )
        store.upsert(lesson)
    results = store.retrieve("test_failure", top_k=5, max_chars=250)
    assert len(results) < 5


def test_unmatched_lessons_decay_and_prune():
    store = LessonStore()
    lesson = extract_lesson(
        _event(),
        lambda ev: {"observation": "o", "prevention": "a sufficiently long prevention string"},
    )
    store.upsert(lesson)
    fp = lesson.fingerprint
    for _ in range(20):  # far more than needed to cross the prune threshold
        store.mark_unmatched([fp])
    assert store.all() == ()


def test_empty_store_retrieves_nothing():
    store = LessonStore()
    assert store.retrieve("anything") == ()
