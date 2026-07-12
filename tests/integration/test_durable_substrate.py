"""Durable-substrate integration tests (opt-in).

Run against the durable compose profile:

    docker compose --profile durable up -d db-durable
    psql postgresql://forge:forge@127.0.0.1:5433/forge \
        -f migrations/003_durable_substrate.sql
    FORGE_PG_EXTENSIONS=1 \
    FORGE_DURABLE_DATABASE_URL=postgresql://forge:forge@127.0.0.1:5433/forge \
        uv run pytest tests/integration/test_durable_substrate.py -q

Skipped entirely on the stock image / in CI without the env gate.
"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_PG_EXTENSIONS", "").lower() not in {"1", "true", "yes", "on"},
    reason="durable substrate experiments need FORGE_PG_EXTENSIONS=1 and the durable profile",
)


@pytest.fixture()
def pg_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = os.getenv("FORGE_DURABLE_DATABASE_URL",
                    "postgresql://forge:forge@127.0.0.1:5433/forge")
    engine = create_engine(url)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_pgmq_backend_detected(pg_db):
    from forge_runtime.durable_queue import DurableQueue
    assert DurableQueue(pg_db).backend() == "pgmq"


def test_pgmq_send_read_archive_roundtrip(pg_db):
    from forge_runtime.durable_queue import DurableQueue
    q = DurableQueue(pg_db)
    msg_id = q.send({"job": "consolidate", "key": "p1:consolidate:test"})
    assert msg_id is not None

    msg = q.read(vt=2)
    assert msg is not None and msg.payload["job"] == "consolidate"
    # Hidden while the visibility timeout holds…
    assert q.read(vt=2) is None or q.read(vt=2).msg_id != msg.msg_id
    assert q.archive(msg.msg_id)


def test_pgmq_crash_redelivery(pg_db):
    """A consumer that dies mid-job (never archives) gets the message back."""
    from forge_runtime.durable_queue import DurableQueue
    q = DurableQueue(pg_db)
    q.send({"job": "backfill", "key": "p1:backfill:crash-test"})
    first = q.read(vt=1)
    assert first is not None
    time.sleep(1.5)  # visibility timeout expires — simulated crash
    again = q.read(vt=30)
    assert again is not None and again.msg_id == first.msg_id
    assert q.archive(again.msg_id)


def test_pg_cron_jobs_registered(pg_db):
    from sqlalchemy import text
    count = pg_db.execute(text(
        "SELECT count(*) FROM cron.job WHERE jobname LIKE 'forge_%'")).scalar()
    assert count >= 2  # archive-expired + decay-lessons from migration 003


def test_vectorscale_index_present_when_available(pg_db):
    from sqlalchemy import text
    available = pg_db.execute(text(
        "SELECT 1 FROM pg_extension WHERE extname = 'vectorscale'")).scalar()
    if not available:
        pytest.skip("vectorscale not installed on this image")
    idx = pg_db.execute(text(
        "SELECT 1 FROM pg_indexes WHERE indexname = "
        "'idx_hermes_memory_items_embedding_diskann'")).scalar()
    assert idx


def test_requested_extension_contract_is_installed(pg_db):
    """The durable image must not silently fall back for requested workers."""
    from sqlalchemy import text
    expected = {
        "vector", "vectorscale", "ai", "pgmq", "pg_cron", "pg_partman",
        "pg_net", "pg_later", "pg_task", "pg_durable",
    }
    installed = {
        row[0] for row in pg_db.execute(text("SELECT extname FROM pg_extension"))
        if row[0] in expected
    }
    assert installed == expected - {"pg_task"}
    preloaded = pg_db.execute(text(
        "SELECT setting FROM pg_settings WHERE name = 'shared_preload_libraries'"
    )).scalar() or ""
    assert "pg_task" in {item.strip() for item in preloaded.split(",")}
    assert pg_db.execute(text("SELECT to_regclass('public.task')")).scalar() == "task"
