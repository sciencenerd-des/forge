# Forge architecture

Forge runs **long-horizon autonomous coding work** with small/local models by
making the loop *boringly reliable*: every cycle is checkpointed to a durable
store, every "done" claim is backed by tests the evaluator runs itself, and no
single bad step can take the whole run down.

## The PGE loop

```
            ┌──────────── durable Postgres state (resume point) ───────────┐
            │                                                              │
   planner ──► auditor ──► executor ──► evaluator ──► (planner | end)
   (decompose) (contract)  (act: edit,  (RUN the
                            build, run)   contract)
```

- **Planner** — picks/decomposes the next task from the active goal. Re-decomposes
  a task that stagnates instead of looping on it.
- **Auditor** — derives a **dual contract** for the goal *once*: an executor
  checklist (human-readable acceptance criteria) and an **evaluator test list**
  (shell commands that are the machine-checkable ground truth). For known stacks
  (cpp/python/node/rust) the contract is **deterministically templated**, not
  LLM-authored, so it is always tech-correct and satisfiable.
- **Executor** — takes one bounded action (write/edit a file, run a build, run a
  command). Tools are policy-enforced and sandboxed to the project workspace.
- **Evaluator** — **independently runs the test list** and returns a verdict from
  the exit codes/output — it never takes the executor's word for "done".

A **steward** assembles a compact context pack each cycle so a small model isn't
drowned in history.

### Batches and persistence

`run_pge.py` drives the graph in a `while`-loop of **batches**. Each batch runs the
graph up to a per-batch turn cap (`PGE_MAX_TURNS`); whatever it accomplishes is
checkpointed to Postgres. The loop then reloads durable state and runs the next
batch. The run ends only on: goal verified complete, a blocked state with no
actionable task, sustained no-progress (stagnation), or an exhausted failure
budget. Crucially, **a batch exception is caught, logged, and retried** under a
consecutive-failure budget (`PGE_MAX_CONSECUTIVE_FAILURES`, default 6) — a
node-level bug costs a retry, not the run.

`pge_launcher.py` starts a run **detached** (`start_new_session=True`) so it
survives the process that launched it (e.g. a chat gateway restart), and tracks a
JSON manifest mirrored into the control-plane DB.

## Data model

The engine persists to **Postgres** (`app/models`, `app/services`): projects,
goals, tasks, memory items (including the `audit_tests` contract rows), file
changes, test runs, checkpoints, events. It relies on `ARRAY` columns, so Postgres
is required (the bundled `docker-compose.yml` provides it).

## Control plane + console

`control_plane/` is a FastAPI service (default `:8787`) that reads the run manifest
and the database and exposes run snapshots (schema in `contracts/`). `web/` is a
React 19 + Vite operator console that proxies `/api` to the control plane.

## Configuration

Everything machine-specific resolves through `forge_config.py` from environment
variables (see `.env.example`): `FORGE_HOME`, workspaces, state/tool DBs,
`DATABASE_URL`, the default project (resolve-or-create — never a hardcoded id), and
per-role **OpenAI-compatible** provider profiles (LM Studio / Ollama / vLLM /
cloud). This is what makes Forge plug-and-play.

## Why it is built this way

See [docs/LESSONS.md](docs/LESSONS.md) — the design invariants are direct scar
tissue from real failure modes, each now pinned by a test in `tests/regression/`.
