-- 004: Research-grounded memory v2 extension contract.
-- Apply this to the durable PG17 profile after the Forge tables exist:
--   psql "$FORGE_DURABLE_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/004_memory_v2_extensions.sql
--
-- Do not rename these components:
--   pg_diskann        -> vectorscale access method (StreamingDiskANN)
--   pg_ai_query       -> ai extension (pgai SQL model/query functions)
--   pg_vectorize      -> pgai-vectorizer-worker service
--   pg_timetable      -> standalone scheduler service
-- pg_partman_bgw is the worker shipped by the pg_partman package, not a
-- separate CREATE EXTENSION name. pg_task is also not a CREATE EXTENSION:
-- upstream ships a preload-only worker (pg_task.so) which creates and
-- maintains its task table when PostgreSQL starts.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
CREATE EXTENSION IF NOT EXISTS ai CASCADE;
CREATE EXTENSION IF NOT EXISTS pgmq;
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_partman;
CREATE EXTENSION IF NOT EXISTS pg_net;
CREATE EXTENSION IF NOT EXISTS pg_later CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_durable;

DO $$
DECLARE
    required text[] := ARRAY[
        'vector', 'vectorscale', 'ai', 'pgmq', 'pg_cron', 'pg_partman',
        'pg_net', 'pg_later', 'pg_durable'
    ];
    missing text;
BEGIN
    SELECT string_agg(name, ', ' ORDER BY name)
      INTO missing
      FROM unnest(required) AS requested(name)
     WHERE NOT EXISTS (
         SELECT 1 FROM pg_extension installed WHERE installed.extname = requested.name
     );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'Forge durable extension contract is incomplete: %', missing;
    END IF;

    IF NOT EXISTS (
        SELECT 1
         FROM pg_settings
         WHERE name = 'shared_preload_libraries'
           AND setting ~ '(^|,)[[:space:]]*pg_task(,|$)'
    ) THEN
        RAISE EXCEPTION 'Forge durable extension contract is incomplete: pg_task is not preloaded';
    END IF;

    IF to_regclass('public.task') IS NULL THEN
        RAISE EXCEPTION 'Forge durable extension contract is incomplete: pg_task task table is unavailable';
    END IF;

    IF current_setting('pg_durable.worker_role', true) IS DISTINCT FROM 'forge' THEN
        RAISE EXCEPTION 'Forge durable extension contract is incomplete: pg_durable worker role is not forge';
    END IF;
END $$;
