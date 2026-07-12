"""Phase 2: deterministic links, one-hop expansion, and safe evolution."""
from datetime import datetime

from app import models as m
from app.services.memory_links import evolve_memories, expand_hits, generate_links


def _project(db):
    project = m.HermesProject(name="p", repo_path="/tmp/p")
    db.add(project)
    db.commit()
    return project


def test_generate_links_is_bidirectional_and_tag_safe(sqlite_db):
    project = _project(sqlite_db)
    first = m.HermesMemoryItem(
        project_id=project.id, memory_type="mistake", content="one",
        tags=["python", "fp:old"], file_path="src/a.py")
    second = m.HermesMemoryItem(
        project_id=project.id, memory_type="mistake", content="two",
        tags=["python"], file_path="src/a.py")
    sqlite_db.add_all([first, second])
    sqlite_db.commit()
    linked = generate_links(sqlite_db, second)
    assert linked == (first.id,)
    sqlite_db.refresh(first)
    sqlite_db.refresh(second)
    assert f"link:{first.id}" in second.tags
    assert f"link:{second.id}" in first.tags
    assert "link:link:" not in second.tags


def test_expand_hits_respects_budget(sqlite_db):
    project = _project(sqlite_db)
    target = m.HermesMemoryItem(project_id=project.id, memory_type="lesson", content="target")
    sqlite_db.add(target)
    sqlite_db.flush()
    source = m.HermesMemoryItem(
        project_id=project.id, memory_type="mistake", content="source",
        tags=[f"link:{target.id}"])
    sqlite_db.add(source)
    sqlite_db.commit()
    assert [row.content for row in expand_hits(sqlite_db, [source], 100)] == ["source", "target"]
    assert [row.content for row in expand_hits(sqlite_db, [source], 8)] == ["source"]


def test_evolution_skips_authority_types_and_updates_older_lessons(sqlite_db):
    project = _project(sqlite_db)
    old = m.HermesMemoryItem(
        project_id=project.id, memory_type="lesson", content="old",
        tags=[], created_at=datetime(2026, 1, 1))
    new = m.HermesMemoryItem(
        project_id=project.id, memory_type="lesson", content="new context",
        tags=[], created_at=datetime(2026, 1, 2))
    sqlite_db.add_all([old, new])
    sqlite_db.commit()
    new.tags = [f"link:{old.id}"]
    sqlite_db.commit()
    assert evolve_memories(sqlite_db, project.id) == 1
    sqlite_db.refresh(old)
    assert "superseded context: new context" in old.content

    constraint = m.HermesMemoryItem(
        project_id=project.id, memory_type="constraint", content="immutable",
        tags=[f"link:{old.id}"], created_at=datetime(2026, 1, 3))
    sqlite_db.add(constraint)
    sqlite_db.commit()
    assert evolve_memories(sqlite_db, project.id) == 0


class _AmbiguousArray(list):
    """Mimics numpy: truthiness raises, iteration works (pgvector on Postgres)."""

    def __bool__(self):
        raise ValueError("The truth value of an array is ambiguous")


def test_generate_links_survives_numpy_like_embeddings(sqlite_db):
    """pgvector returns numpy arrays on Postgres; `if embedding` raised and the
    best-effort wrapper silently ate every link. Identity checks must be used."""
    project = _project(sqlite_db)
    vec = _AmbiguousArray([0.1] * 8)
    old = m.HermesMemoryItem(project_id=project.id, memory_type="mistake",
                             content="old", tags=["csv"], file_path="parser.py")
    sqlite_db.add(old)
    sqlite_db.commit()
    new = m.HermesMemoryItem(project_id=project.id, memory_type="mistake",
                             content="new", tags=["csv"], file_path="parser.py")
    sqlite_db.add(new)
    sqlite_db.commit()
    # Simulate the ORM handing back array-like embeddings.
    old.embedding = vec
    new.embedding = vec
    linked = generate_links(sqlite_db, new)
    assert linked == (old.id,)
