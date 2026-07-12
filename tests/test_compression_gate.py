"""Compression size gate, bounded steward cache, snapshot adapter, hygiene isolation."""
import pytest

from app import models as m
from app.context_compression import compress_context_pack


@pytest.fixture()
def project(sqlite_db):
    p = m.HermesProject(name="proj", repo_path="/tmp/proj")
    sqlite_db.add(p)
    sqlite_db.commit()
    return p


def test_small_pack_skips_compression_entirely(sqlite_db, project, monkeypatch):
    """The common case (small pack) must not touch Headroom OR the snapshot
    table — previously every executor turn paid both."""
    monkeypatch.delenv("PGE_CONTEXT_COMPACT_THRESHOLD", raising=False)
    pack = {
        "PROJECT": {"goal": "g"},
        "ACTIVE_TASK": {"title": "t"},
        "LESSONS_AND_MISTAKES": ["small"],
    }
    out = compress_context_pack(sqlite_db, project_id=project.id,
                                goal_id=None, task_id=None, pack=pack)
    assert out["CONTEXT_COMPRESSION"]["status"] == "not_needed"
    assert out["CONTEXT_COMPRESSION"]["compressor"] == "size-gate"
    assert sqlite_db.query(m.HermesContextCompressionSnapshot).count() == 0
    assert out["LESSONS_AND_MISTAKES"] == ["small"]


def test_oversized_pack_still_compacts_via_fallback(sqlite_db, project, monkeypatch):
    monkeypatch.setenv("PGE_CONTEXT_COMPACT_THRESHOLD", "10")  # ~40 chars
    pack = {
        "PROJECT": {"goal": "g"},
        "BULK_HISTORY": ["entry-%d" % i for i in range(50)],
    }
    out = compress_context_pack(sqlite_db, project_id=project.id,
                                goal_id=None, task_id=None, pack=pack)
    status = out["CONTEXT_COMPRESSION"].get("status", "")
    # Headroom absent -> deterministic local compaction; present -> snapshot.
    assert status in {"local_compacted"} or "snapshot_id" in out["CONTEXT_COMPRESSION"]
    assert out["PROJECT"] == {"goal": "g"}  # protected key untouched


def test_protected_keys_never_compressed(sqlite_db, project, monkeypatch):
    monkeypatch.setenv("PGE_CONTEXT_COMPACT_THRESHOLD", "1")
    constraints = ["constraint-%d" % i for i in range(100)]
    pack = {"NON_NEGOTIABLE_CONSTRAINTS": constraints,
            "BULK": ["x" * 50] * 100}
    out = compress_context_pack(sqlite_db, project_id=project.id,
                                goal_id=None, task_id=None, pack=pack)
    assert out["NON_NEGOTIABLE_CONSTRAINTS"] == constraints


# ---------------------------------------------------------------------------
# Steward: bounded cache + pack-derived snapshot
# ---------------------------------------------------------------------------

def test_brief_cache_is_bounded():
    from src import steward
    steward._BRIEF_CACHE.clear()
    for i in range(100):
        steward._brief_cache_put(("p", f"task-{i}"), ("fp", "brief"))
    assert len(steward._BRIEF_CACHE) == steward._BRIEF_CACHE_MAX
    # Newest entries survive; oldest were evicted.
    assert ("p", "task-99") in steward._BRIEF_CACHE
    assert ("p", "task-0") not in steward._BRIEF_CACHE


def test_snapshot_from_pack_shape():
    from src.steward import snapshot_from_pack
    pack = {
        "PROJECT": {"goal": "ship it", "status": "active"},
        "ACTIVE_TASK": {"title": "t1", "status": "active",
                        "acceptance_criteria": ["passes tests"]},
        "RELEVANT_FILES": [{"file_path": "a.py", "summary": "added foo"}],
    }
    snap = snapshot_from_pack(pack)
    assert snap["goal"]["title"] == "ship it"
    assert snap["goal"]["criteria"] == ["passes tests"]
    assert snap["tasks"] == [{"title": "t1", "status": "active"}]
    assert snap["recent_files"] == ["a.py: added foo"]
    assert snap["recent_tests"] == []


def test_compact_context_uses_supplied_snapshot(monkeypatch):
    """With a snapshot supplied, the steward must not query the DB at all."""
    from src import steward
    steward._BRIEF_CACHE.clear()

    def no_db(project_id):
        raise AssertionError("DB snapshot must not be fetched when one is supplied")

    monkeypatch.setattr(steward, "_db_snapshot", no_db)
    monkeypatch.setattr(steward, "_chat", lambda *a, **kw: "BRIEFING: all good")
    out = steward.compact_context("pid", "t", snapshot={"goal": None, "tasks": []})
    assert out == "all good"


# ---------------------------------------------------------------------------
# Hygiene: failures are isolated, never propagated
# ---------------------------------------------------------------------------

def test_batch_hygiene_swallow_all_failures(monkeypatch):
    import run_pge

    class ExplodingSessionLocal:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(run_pge, "SessionLocal", ExplodingSessionLocal())
    # Must not raise — hygiene can never take a batch down.
    with pytest.raises(Exception):
        # sanity: the session factory itself raises...
        run_pge.SessionLocal()
    assert run_pge.run_batch_hygiene("pid", 1) is None


def test_batch_hygiene_runs_on_sqlite(monkeypatch, sqlite_db, project):
    """End-to-end on SQLite with embeddings disabled: no exception, no growth."""
    import run_pge
    monkeypatch.setenv("FORGE_EMBED_ENABLED", "false")

    class FakeSessionLocal:
        def __call__(self):
            return sqlite_db

    monkeypatch.setattr(run_pge, "SessionLocal", FakeSessionLocal())
    # closing the shared fixture session would break the fixture teardown
    monkeypatch.setattr(sqlite_db, "close", lambda: None)
    assert run_pge.run_batch_hygiene(project.id, 4) is None
