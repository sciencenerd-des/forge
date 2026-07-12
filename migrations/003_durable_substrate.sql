-- 003: legacy durable-substrate helpers (opt-in). Every statement is guarded
-- so this file remains safe on the stock pgvector image. The PG17 durable
-- profile's fail-closed extension contract is migrations/004_memory_v2_extensions.sql;
-- apply that file after this one when all requested components are required.
--
-- Enables (when available):
--   pgmq          durable hygiene/work queue with visibility timeouts
--   pg_cron       in-database schedules that run with no Forge process alive
--   pg_partman    monthly partitions for the append-heavy event/log tables
--   vectorscale   StreamingDiskANN index over hermes_memory_items.embedding

DO $$
BEGIN
    -- ------------------------------------------------------------------ pgmq
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgmq') THEN
        CREATE EXTENSION IF NOT EXISTS pgmq;
        IF NOT EXISTS (
            SELECT 1 FROM pgmq.list_queues() WHERE queue_name = 'forge_hygiene'
        ) THEN
            PERFORM pgmq.create('forge_hygiene');
        END IF;
    ELSE
        RAISE NOTICE 'pgmq not available — durable queue uses the plain-table fallback';
    END IF;

    -- --------------------------------------------------------------- pg_cron
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        CREATE EXTENSION IF NOT EXISTS pg_cron;
        -- Pure-SQL hygiene owned by the database. run_pge.py detects rows in
        -- cron.job named forge_% and skips its app-side duplicates.
        PERFORM cron.unschedule(jobid)
          FROM cron.job
         WHERE jobname = 'forge_archive_expired_memory';
        PERFORM cron.schedule('forge_archive_expired_memory', '17 * * * *', $sql$
            UPDATE hermes_memory_items SET status = 'archived'
            WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < now();
        $sql$);
        PERFORM cron.unschedule(jobid)
          FROM cron.job
         WHERE jobname = 'forge_decay_stale_lessons';
        PERFORM cron.schedule('forge_decay_stale_lessons', '23 3 * * *', $sql$
            UPDATE hermes_memory_items
            SET confidence = confidence - 0.05,
                status = CASE WHEN confidence - 0.05 < 0.3 THEN 'archived' ELSE status END
            WHERE memory_type = 'lesson' AND status = 'active'
              AND updated_at < now() - interval '1 day';
        $sql$);
    ELSE
        RAISE NOTICE 'pg_cron not available — hygiene stays app-side at batch boundaries';
    END IF;

    -- ------------------------------------------------------------ pg_partman
    -- Native partitioning requires the parent to be partitioned at creation;
    -- converting live tables is done via partman's documented offline steps.
    -- Here we only register maintenance if the parent is already partitioned.
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_partman') THEN
        CREATE EXTENSION IF NOT EXISTS pg_partman;
        IF EXISTS (SELECT 1 FROM pg_partitioned_table pt
                   JOIN pg_class c ON c.oid = pt.partrelid
                   WHERE c.relname = 'hermes_events') THEN
            PERFORM partman.create_parent(
                p_parent_table := 'public.hermes_events',
                p_control := 'created_at',
                p_interval := '1 month');
        END IF;
    ELSE
        RAISE NOTICE 'pg_partman not available — event tables remain unpartitioned';
    END IF;

    -- ----------------------------------------------------------- vectorscale
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vectorscale') THEN
        CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
        -- StreamingDiskANN over memory embeddings (cosine — matches the
        -- retrieval operator <=> used by app/services/memory_retrieval.py).
        IF NOT EXISTS (SELECT 1 FROM pg_indexes
                       WHERE indexname = 'idx_hermes_memory_items_embedding_diskann') THEN
            CREATE INDEX idx_hermes_memory_items_embedding_diskann
                ON hermes_memory_items USING diskann (embedding vector_cosine_ops);
        END IF;
    ELSE
        RAISE NOTICE 'vectorscale not available — pgvector seq/HNSW scan remains';
    END IF;
END $$;
