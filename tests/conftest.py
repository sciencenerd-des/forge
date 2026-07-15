"""Shared pytest setup for Forge.

Puts the two package roots (repo root for ``app``/``control_plane``/``forge_config``
and ``engine`` for ``src``/``forge_runtime.llm``) on ``sys.path`` and points
``FORGE_HOME`` at a throwaway temp dir so tests never touch real state.
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ENGINE = _ROOT / "engine"
for p in (str(_ROOT), str(_ENGINE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Isolate all filesystem state into a temp home for the whole test session.
os.environ.setdefault("FORGE_HOME", tempfile.mkdtemp(prefix="forge-test-home-"))

import pytest


@pytest.fixture()
def sqlite_db():
    """Fresh in-memory SQLite session over the Hermes schema.

    Postgres-only functional (tsvector) indexes are stripped before
    create_all; ARRAY/vector columns render via their SQLite JSON variants.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — registers tables on Base.metadata
    from app.database import Base

    engine = create_engine("sqlite://")
    for table in Base.metadata.tables.values():
        table.indexes = {ix for ix in table.indexes
                         if "tsvector" not in (ix.name or "")}
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
