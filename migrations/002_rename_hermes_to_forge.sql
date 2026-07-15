-- Upgrade the engine schema before starting code that uses Forge* metadata.
-- Run transactionally after 001/create_all has established the legacy tables.
BEGIN;

DO $$
DECLARE
  table_name text;
  forge_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'projects', 'goals', 'tasks', 'events', 'memory_items', 'file_changes',
    'test_runs', 'checkpoints', 'context_pack_logs', 'context_compression_snapshots',
    'runtime_metadata'
  ] LOOP
    forge_name := 'forge_' || table_name;
    IF to_regclass('public.hermes_' || table_name) IS NOT NULL
       AND to_regclass('public.' || forge_name) IS NULL THEN
      EXECUTE format('ALTER TABLE public.%I RENAME TO %I', 'hermes_' || table_name, forge_name);
    END IF;
  END LOOP;
END $$;

-- Compatibility views are read-only by design; they let external legacy
-- readers survive one release without keeping Hermes names in ORM metadata.
DO $$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'projects', 'goals', 'tasks', 'events', 'memory_items', 'file_changes',
    'test_runs', 'checkpoints', 'context_pack_logs', 'context_compression_snapshots',
    'runtime_metadata'
  ] LOOP
    IF to_regclass('public.hermes_' || table_name) IS NULL
       AND to_regclass('public.forge_' || table_name) IS NOT NULL THEN
      EXECUTE format('CREATE VIEW public.%I AS SELECT * FROM public.%I', 'hermes_' || table_name, 'forge_' || table_name);
    END IF;
  END LOOP;
END $$;

COMMIT;
