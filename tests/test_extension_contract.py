"""Static checks for the requested extension installation contract."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_durable_manifest_maps_non_extension_names_explicitly():
    manifest = (ROOT / "docker/postgres-durable/extension-manifest.env").read_text()
    assert "PG_DISKANN_COMPONENT=vectorscale" in manifest
    assert "PG_AI_QUERY_COMPONENT=ai" in manifest
    assert "PG_VECTORIZE_COMPONENT=pgai-vectorizer-worker" in manifest
    assert "PGTIMETABLE_VERSION=v6.3.0" in manifest


def test_extension_migration_is_fail_closed_and_does_not_fake_names():
    sql = (ROOT / "migrations/004_memory_v2_extensions.sql").read_text()
    for extension in (
        "vector", "vectorscale", "ai", "pgmq", "pg_cron", "pg_partman",
        "pg_net", "pg_later", "pg_durable",
    ):
        assert f"CREATE EXTENSION IF NOT EXISTS {extension}" in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_task" not in sql
    assert "pg_task is not preloaded" in sql
    assert "pg_task task table is unavailable" in sql
    assert "pg_durable worker role is not forge" in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_diskann" not in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_vectorize" not in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_timetable" not in sql


def test_compose_has_separate_vectorizer_and_timetable_services():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "pgai-vectorizer:" in compose
    assert "pg-timetable:" in compose
    assert "PGAI_VECTORIZER_WORKER_DB_URL" in compose


def test_durable_migration_is_rerunnable_for_queue_and_cron_jobs():
    sql = (ROOT / "migrations/003_durable_substrate.sql").read_text()
    assert "pgmq.list_queues()" in sql
    assert "cron.unschedule(jobid)" in sql
