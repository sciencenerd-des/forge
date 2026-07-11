"""Durable lessons: dedupe on fingerprint, decay-to-archive, budgeted retrieval."""
import pytest

from app import models as m
from forge_runtime.lesson_store_db import MEMORY_TYPE, DbLessonStore
from forge_runtime.steering import Lesson


@pytest.fixture()
def project(sqlite_db):
    p = m.HermesProject(name="proj", repo_path="/tmp/proj")
    sqlite_db.add(p)
    sqlite_db.commit()
    return p


def _lesson(fp="f" * 16, failure_type="verification", confidence=0.9,
            observation="test t1 keeps failing on missing header",
            prevention="run make test before claiming done and fix t1 first"):
    return Lesson(fingerprint=fp, task_id="", failure_type=failure_type,
                  observation=observation, prevention=prevention,
                  evidence_ids=("t1",), confidence=confidence)


def test_upsert_dedupes_and_bumps_confidence(sqlite_db, project):
    store = DbLessonStore(sqlite_db, project.id)
    store.upsert(_lesson())
    bumped = store.upsert(_lesson())
    rows = sqlite_db.query(m.HermesMemoryItem).filter_by(
        memory_type=MEMORY_TYPE).all()
    assert len(rows) == 1
    assert bumped.confidence == pytest.approx(0.95)


def test_lessons_survive_across_store_instances(sqlite_db, project):
    DbLessonStore(sqlite_db, project.id).upsert(_lesson())
    # a "new run" builds a fresh store over the same durable session/DB
    fresh = DbLessonStore(sqlite_db, project.id)
    lessons = fresh.all()
    assert len(lessons) == 1
    assert lessons[0].prevention.startswith("run make test")


def test_decay_archives_instead_of_deleting(sqlite_db, project):
    store = DbLessonStore(sqlite_db, project.id)
    store.upsert(_lesson(confidence=0.32))
    store.mark_unmatched([_lesson().fingerprint])  # 0.32 - 0.05 < 0.3
    assert store.all() == ()  # invisible to retrieval
    row = sqlite_db.query(m.HermesMemoryItem).filter_by(
        memory_type=MEMORY_TYPE).one()  # but still auditable
    assert row.status == "archived"


def test_recurrence_resurrects_archived_lesson(sqlite_db, project):
    store = DbLessonStore(sqlite_db, project.id)
    store.upsert(_lesson(confidence=0.31))
    store.mark_unmatched([_lesson().fingerprint])
    assert store.all() == ()
    store.upsert(_lesson())  # the failure recurred → lesson is live again
    assert len(store.all()) == 1


def test_retrieve_ranks_by_confidence_and_respects_budget(sqlite_db, project):
    store = DbLessonStore(sqlite_db, project.id)
    store.upsert(_lesson(fp="a" * 16, confidence=0.5,
                         observation="low", prevention="p" * 30))
    store.upsert(_lesson(fp="b" * 16, confidence=0.9,
                         observation="high", prevention="q" * 30))
    got = store.retrieve("verification", top_k=1)
    assert len(got) == 1 and got[0].observation == "high"

    # An over-budget lesson is skipped rather than blowing the cap.
    tight = store.retrieve("verification", top_k=3, max_chars=10)
    assert tight == ()


def test_retrieve_filters_by_failure_type_prefix(sqlite_db, project):
    store = DbLessonStore(sqlite_db, project.id)
    store.upsert(_lesson(fp="a" * 16, failure_type="verification.pytest"))
    store.upsert(_lesson(fp="b" * 16, failure_type="sandbox.denied"))
    got = store.retrieve("verification")
    assert len(got) == 1 and got[0].failure_type == "verification.pytest"
