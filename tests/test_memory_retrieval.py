"""Task-aware memory retrieval: ranking, scope, budget, and embedding config."""
import pytest

from app import models as m
from app.services.memory_retrieval import _apply_char_budget, _terms, select_memories


@pytest.fixture()
def project(sqlite_db):
    p = m.HermesProject(name="proj", repo_path="/tmp/proj")
    sqlite_db.add(p)
    sqlite_db.commit()
    return p


@pytest.fixture()
def goal_task(sqlite_db, project):
    g = m.HermesGoal(project_id=project.id, title="ship raytracer")
    sqlite_db.add(g)
    sqlite_db.commit()
    t = m.HermesTask(project_id=project.id, goal_id=g.id,
                     title="implement sphere intersection", status="active")
    sqlite_db.add(t)
    sqlite_db.commit()
    return g, t


def _mem(db, project, task_id=None, **kw):
    defaults = dict(project_id=project.id, task_id=task_id,
                    memory_type="decision", content="x", status="active",
                    importance=3, confidence=0.8, tags=[])
    defaults.update(kw)
    item = m.HermesMemoryItem(**defaults)
    db.add(item)
    db.commit()
    return item


def test_scope_filters_preserved(sqlite_db, project, goal_task):
    _, task = goal_task
    in_scope = _mem(sqlite_db, project, task.id, content="task memory")
    global_constraint = _mem(sqlite_db, project, None,
                             memory_type="constraint", content="never rm -rf")
    _mem(sqlite_db, project, None, content="orphan decision")  # out of scope
    inactive = _mem(sqlite_db, project, task.id, content="old", status="archived")

    got = select_memories(sqlite_db, project.id, [task.id], "sphere")
    ids = {r.id for r in got}
    assert in_scope.id in ids
    assert global_constraint.id in ids
    assert inactive.id not in ids
    assert len(ids) == 2


def test_superseded_items_excluded(sqlite_db, project, goal_task):
    _, task = goal_task
    old = _mem(sqlite_db, project, task.id, content="v1 decision")
    _mem(sqlite_db, project, task.id, content="v2 decision", supersedes_id=old.id)
    got = select_memories(sqlite_db, project.id, [task.id], "")
    contents = [r.content for r in got]
    assert "v2 decision" in contents
    assert "v1 decision" not in contents


def test_recency_tier_orders_by_importance(sqlite_db, project, goal_task):
    _, task = goal_task
    _mem(sqlite_db, project, task.id, content="minor note", importance=1)
    _mem(sqlite_db, project, task.id, content="critical constraint", importance=5)
    got = select_memories(sqlite_db, project.id, [task.id], "")
    assert got[0].content == "critical constraint"


def test_char_budget_caps_but_keeps_top_item():
    class Item:
        def __init__(self, content):
            self.content = content

    items = [Item("a" * 5000), Item("b" * 5000), Item("c" * 10)]
    kept = _apply_char_budget(items, char_budget=5010)
    assert [len(i.content) for i in kept] == [5000, 10]

    # A single over-budget item is still returned — never an empty pack.
    kept = _apply_char_budget([Item("z" * 9999)], char_budget=100)
    assert len(kept) == 1


def test_terms_filters_stopwords():
    terms = _terms("Fix the failing sphere intersection in raytracer.py")
    assert "sphere" in terms and "raytracer.py" in terms
    assert "the" not in terms and "fix" in terms


def test_embed_query_not_called_on_sqlite(sqlite_db, project, goal_task):
    """Tier 1 is Postgres-only; the embed callable must not fire on SQLite."""
    _, task = goal_task
    _mem(sqlite_db, project, task.id, content="something")
    calls = []
    select_memories(sqlite_db, project.id, [task.id], "sphere",
                    embed_query=lambda text: calls.append(text) or [0.0] * 768)
    assert calls == []


def test_context_pack_records_selected_lesson_ids(sqlite_db, project, goal_task, monkeypatch):
    from app.services import MemoryService

    _, task = goal_task
    lesson = _mem(
        sqlite_db, project, task.id, memory_type="lesson",
        content="run the failing verification command before retrying",
    )
    monkeypatch.setenv("PGE_HEADROOM_ENABLED", "false")
    pack = MemoryService(sqlite_db).build_context_pack(project.id)
    assert pack["SELECTED_LESSON_IDS"] == [lesson.id]


# ---------------------------------------------------------------------------
# Phase 2: embedding config + backfill
# ---------------------------------------------------------------------------

def test_embedding_provider_env_routing(monkeypatch):
    import forge_config
    monkeypatch.setenv("FORGE_EMBED_BASE_URL", "http://embed.host:9999/v1/")
    monkeypatch.setenv("FORGE_EMBED_MODEL", "my-embedder")
    profile = forge_config.embedding_provider()
    assert profile["base_url"] == "http://embed.host:9999/v1"
    assert profile["model"] == "my-embedder"
    assert profile["enabled"] is True


def test_generate_embedding_uses_profile(monkeypatch, sqlite_db):
    from app.services import MemoryService
    monkeypatch.setenv("FORGE_EMBED_BASE_URL", "http://embed.host:9999/v1")
    monkeypatch.setenv("FORGE_EMBED_MODEL", "my-embedder")
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"embedding": [0.1] * 768}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, model=json["model"])
        return FakeResponse()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    emb = MemoryService(sqlite_db)._generate_embedding("hello")
    assert emb == [0.1] * 768
    assert seen["url"] == "http://embed.host:9999/v1/embeddings"
    assert seen["model"] == "my-embedder"


def test_embedding_disabled_short_circuits(monkeypatch, sqlite_db):
    from app.services import MemoryService
    monkeypatch.setenv("FORGE_EMBED_ENABLED", "false")

    def boom(*a, **kw):  # any HTTP call is a failure
        raise AssertionError("HTTP must not be called when embeddings are disabled")

    import requests
    monkeypatch.setattr(requests, "post", boom)
    assert MemoryService(sqlite_db)._generate_embedding("hello") is None


def test_backfill_circuit_breaker(monkeypatch, sqlite_db, project, goal_task):
    """A dead endpoint costs ONE probe, not one per row."""
    from app.services import MemoryService
    _, task = goal_task
    for i in range(5):
        _mem(sqlite_db, project, task.id, content=f"m{i}")
    service = MemoryService(sqlite_db)
    calls = []
    monkeypatch.setattr(service, "_generate_embedding",
                        lambda text: calls.append(text) or None)
    assert service.backfill_embeddings(project.id) == 0
    assert len(calls) == 1


def test_backfill_updates_rows(monkeypatch, sqlite_db, project, goal_task):
    from app.services import MemoryService
    _, task = goal_task
    rows = [_mem(sqlite_db, project, task.id, content=f"m{i}") for i in range(3)]
    service = MemoryService(sqlite_db)
    monkeypatch.setattr(service, "_generate_embedding", lambda text: [0.5] * 768)
    assert service.backfill_embeddings(project.id) == 3
    for row in rows:
        sqlite_db.refresh(row)
        assert row.embedding is not None


# ---------------------------------------------------------------------------
# Phase 1: reranker gate + process-level cache
# ---------------------------------------------------------------------------

def test_rerank_gate_off_never_loads_model(monkeypatch, sqlite_db):
    import app.services as services
    monkeypatch.delenv("FORGE_MEMORY_RERANK", raising=False)

    def boom():
        raise AssertionError("reranker must not load when the gate is off")

    monkeypatch.setattr(services, "_cached_reranker", boom)

    class Item:
        content = "c"

    items = [Item(), Item(), Item()]
    got = services.MemoryService(sqlite_db)._rerank_results("q", items, limit=2)
    assert got == items[:2]


def test_rerank_gate_on_missing_model_degrades(monkeypatch, sqlite_db):
    import app.services as services
    monkeypatch.setenv("FORGE_MEMORY_RERANK", "1")
    monkeypatch.setattr(services, "_cached_reranker", lambda: None)

    class Item:
        content = "c"

    items = [Item(), Item()]
    got = services.MemoryService(sqlite_db)._rerank_results("q", items, limit=1)
    assert got == items[:1]
