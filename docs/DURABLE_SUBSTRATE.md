# Durable Postgres substrate — extension triage & experiment log

Forge's core bet is that durability lives in Postgres (see ARCHITECTURE.md).
This log records which extensions we evaluated for pushing more durability
primitives *into* the database, the verdict for each, and the experiment
results. Everything is opt-in behind the `durable` compose profile +
`FORGE_PG_EXTENSIONS=1`; the app-side paths remain the default and fallback.

The installable profile is PostgreSQL 17 (`docker/postgres-durable`). Build it
with `make db-durable`, then apply `make db-extensions` after Forge has created
its tables. The image build fails if a requested backend control file (or the
`pg_task` preload library) is missing; the SQL migration fails if any required
extension or preload contract is not actually active. This avoids treating a
fallback path as an installed extension.

Grounding: Sagas (Garcia-Molina & Salem 1987), DBOS (SOSP 2023), Temporal's
durable execution (VLDB 2023), Durable Functions (OOPSLA 2021), Beldi
(OSDI 2020), Generative Agents (Park et al. 2023), DPM (arXiv 2026).

## Triage matrix

| Extension | What it gives Forge | Verdict |
|---|---|---|
| **pgmq** | In-database queue: visibility timeouts, archive, crash-safe redelivery | **Adopted (scaffolded).** `forge_runtime/durable_queue.py`; hygiene jobs become survivable messages. SQL-only extension since 1.x — trivial to ship. |
| **pg_cron** | In-database cron; hygiene runs with zero Forge processes alive | **Adopted (scaffolded).** `migrations/003_durable_substrate.sql` schedules `forge_archive_expired_memory` + `forge_decay_stale_lessons`; `run_pge.run_batch_hygiene` detects `cron.job` rows named `forge_%` and stands down. |
| **pg_partman** (+bgw) | Monthly partitions for append-heavy `hermes_events` / `hermes_context_pack_logs` | **Adopted, deferred activation.** Maintenance registered only if the parent is already partitioned; converting live tables is an offline step. Benchmark before/after at ≥1M rows, record here. |
| **pgvector** | The embedding store itself | **Already in production.** Retrieval now actually uses it (task-aware tier 1 in `app/services/memory_retrieval.py`). |
| **pgvectorscale / pg_diskann** | `USING diskann` StreamingDiskANN index over `vector(768)` | **Installed as `vectorscale`.** `pg_diskann` is not a self-hosted extension name; the requested capability maps to Timescale's `vectorscale` access method. |
| **pg_vectorize** | Automated embedding generation + sync | **Installed as the separate `pgai-vectorizer` worker service.** It is not a PostgreSQL extension or preload library; app-side embeddings remain the fallback when the worker is absent. |
| **pg_ai_query** | SQL-side model calls / AI query helpers | **Installed as pgai's `ai` extension.** It remains operator tooling only; model-generated SQL is not placed on the Forge execution path. |
| **pg_net** | Async HTTP from SQL | **Installed and preloaded in the durable profile.** It is opt-in and not called by the loop. |
| **pg_later / pglater** | Async query execution | **Installed as `pg_later` and preloaded.** It depends on pgmq and is not required by the default app path. |
| **pg_task** | Background SQL task worker | **Installed and preloaded.** Upstream ships no `CREATE EXTENSION` control file; the image verifies `pg_task.so`, preload activation, and the worker-owned `public.task` table. It is separate from pg_timetable. |
| **pg_durable** | Durable procedures and background execution | **Installed and preloaded in PG17.** Its worker/database GUCs are configured in the durable image. |
| **pg_boss** | Reliable job queue | **Not an extension** — Node.js library. Recorded as an alternative for JS deployments only. |
| **pg_timetable** | Task-chain scheduler | **Installed as the `pg-timetable` durable-profile service.** It is a standalone Go binary, not a `CREATE EXTENSION` name. |

## Experiment log

### A — pgmq durable hygiene queue
- Status: **scaffolded** (`forge_runtime/durable_queue.py`, queue `forge_hygiene`).
- Fallback proven by unit-style behavior on SQLite (send → read hides for
  `vt` seconds → archive finalizes). The pgmq path is covered by
  `tests/integration/test_durable_substrate.py` (6/6 passed on the PG17 durable
  profile with `FORGE_PG_EXTENSIONS=1`).
- Crash semantics: consumer death ⇒ message reappears after `vt`; consumers
  key side effects (`project_id:job_type:day`) so redelivery is idempotent
  (Beldi-style transactional step).
- Verdict: pending integration run numbers.

### B — pg_cron in-DB scheduling
- Status: **scaffolded** (two `forge_%` jobs in migration 003; app-side
  stand-down implemented in `run_pge.run_batch_hygiene`).
- Verification: migration 003 is rerunnable; it keeps one queue and one row per
  named `forge_%` job by checking the queue and unscheduling before rescheduling.

### C — pg_partman event partitioning
- Status: **registered, not activated** (needs offline conversion of
  `hermes_events` to a partitioned parent).
- Benchmark protocol: synthesize ≥1M events, measure `search_events` and
  `consolidate_old_logs` latency before/after; record here.
- Verdict: pending.

### D — event-sourced replay spike (paper study)
- Question: can `AgentState` be reconstructed by replaying `hermes_events`
  (Temporal/Durable-Functions event-history; DPM task-conditioned projection)
  instead of trusting checkpoint rows?
- Notes so far: `hermes_file_changes` already functions as a saga intent log
  for file side effects; test runs are re-derivable (the evaluator re-runs
  them anyway — replay of *verdicts* is unnecessary by design).
- Verdict: pending prototype (`replay_state(project_id, run_id)` read-only diff).

### E — pg_vectorize automated embeddings
- The requested worker is now declared as `pgai-vectorizer` in the durable
  compose profile. It is deliberately separate from the database backend;
  app-side `_generate_embedding` + `backfill_embeddings` remain the fallback.
- Before enabling a vectorizer for a table, verify the embedding endpoint is
  reachable from the worker and that failed embeddings do not abort inserts.

### F — pgvectorscale StreamingDiskANN
- Status: **guarded index in migration 003**; retrieval SQL unchanged
  (`<=>` cosine is what diskann accelerates).
- Benchmark protocol: ≥100k synthetic memory rows, top-20 cosine query
  latency with/without the index; record recall\@20 as well (SBQ is lossy).
- Verdict: pending; expected "no measurable win at current scale, keep for
  headroom".

### G — extension installation contract
- The PG17 image pins and builds `pgmq`, `pg_cron`, `pg_partman`/`pg_partman_bgw`,
  `pg_net`, `pg_task`, `pg_later`, `pg_durable`, `vectorscale`, and pgai's `ai`
  extension. Build-time control-file verification and migration 004 make
  missing components fail closed.
- `pg_timetable` and pgai Vectorizer run as separate durable-profile services;
  neither is incorrectly represented as a PostgreSQL extension.

### H — live model validation (2026-07-11, pre-PR#4)

Smoke of every PR#4 memory path against real backends (PG17 durable profile,
LM Studio nomic embeddings on :1234, Ollama on :11434):

| Backend | Result | Steward briefing | Steering directive |
|---|---|---|---|
| `gemma4:12b-mlx` (local) | 10/10 PASS | 7.8s, 303 chars, on-goal | 2.0s, correct fix directive |
| `minimax-m3` (Ollama cloud) | 10/10 PASS | 5.7s, 680 chars, on-goal | 3.8s, correct fix directive |
| `glm-5.2` (Ollama cloud) | **untestable** — requires an Ollama subscription upgrade | — | — |

Verified live: embeddings generated on write; tier-1 vector retrieval returns
the task-relevant memory first; EXPLAIN confirms the **diskann** index is used;
pgmq send→read→archive roundtrip; DbLessonStore upsert bump (0.90→0.95) on PG;
compression gate reports `not_needed` for small packs; pack telemetry row
written per build.

Bug found and fixed by this validation: `generate_links` used truthiness on
embeddings (`if item.embedding and ...`); pgvector returns numpy arrays on
Postgres, so every link was silently swallowed by the best-effort wrapper —
invisible to SQLite unit tests. Fixed with identity checks + a numpy-like
regression test (`test_generate_links_survives_numpy_like_embeddings`).
Post-fix: bidirectional links confirmed live on PG17.
