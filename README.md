# Forge

[![CI](https://github.com/sciencenerd-des/forge/actions/workflows/ci.yml/badge.svg)](https://github.com/sciencenerd-des/forge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

![Forge: keep coding agents running and honest](docs/marketing/assets/forge-og.svg)

**A plug-and-play harness for long-running autonomous coding — with local or cloud models.**

Forge runs a **Planner → Auditor → Executor → Evaluator (PGE)** loop that builds real
software with explicit operator and test gates. It is designed for *small* models (it runs happily on a local
LM Studio / Ollama box) by making the loop reliable rather than clever: every cycle is
checkpointed to a durable store, every "done" is backed by tests the evaluator runs
itself, and no single failed step can take the run down.

> Forge is the open-source extraction of a harness that was hardened over weeks of real
> autonomous runs. Every failure mode we hit is now pinned by a test in
> [`tests/regression/`](tests/regression) — see [docs/LESSONS.md](docs/LESSONS.md).

## Why Forge

- **Truthful completion.** A goal is done only when the evaluator *independently runs*
  the contract's test list and every test passes — never on the model's say-so.
- **Resilient by design.** Durable Postgres state means a node-level bug or a model
  timeout costs a retry, not the whole run.
- **Any OpenAI-compatible backend.** LM Studio, Ollama, vLLM, or a cloud API — one
  `base_url` + `model` + key. Route different models to different loop roles.
- **Plug-and-play.** Nothing is wired to one machine; everything resolves from config.
- **Observable.** A FastAPI control plane + React console show runs, events, and verdicts.

## Quickstart

Requirements: Python 3.11+, Docker (for Postgres), and a running model backend
(e.g. [LM Studio](https://lmstudio.ai) serving an OpenAI-compatible API on `:1234`).

```bash
git clone https://github.com/sciencenerd-des/forge && cd forge
make setup          # venv + install + create .env
# edit .env: point LLM_BASE_URL / LLM_MODEL at your backend
make db             # start Postgres (docker)
docker build -t forge-sandbox:latest -f docker/sandbox/Dockerfile .  # one-time: the sandbox image

forge run --goal "Build a Python snake game with tests"
```

The opt-in PG17 durable extension profile is separate from the default PG16
database. Build and verify it only when you need the requested database
workers:

```bash
make db-durable
# first let Forge create its tables, then install and verify every component
make db-extensions
docker compose --profile durable up -d pg-timetable pgai-vectorizer
# optional live contract check (requires the durable profile and psycopg)
FORGE_PG_EXTENSIONS=1 FORGE_DURABLE_DATABASE_URL=postgresql://forge:forge@127.0.0.1:5433/forge \
  uv run pytest tests/integration -q
```

The names map as follows: `pg_diskann` is Timescale `vectorscale`,
`pg_ai_query` is pgai's `ai` extension, and `pg_vectorize`/`pg_timetable` are
separate worker services rather than PostgreSQL extensions. See
[`docs/DURABLE_SUBSTRATE.md`](docs/DURABLE_SUBSTRATE.md) and
[`migrations/004_memory_v2_extensions.sql`](migrations/004_memory_v2_extensions.sql).

Every tool call above runs inside a per-project **sandbox container** by default —
non-root, every capability dropped, files on a Docker-managed volume with no path
back to your host filesystem (see [Sandbox](#sandbox--observability) below). No
`FORGE_ALLOW_HOST_EXECUTION` needed. Container mode fails closed if Docker is not
available. Host mode is an explicit opt-in: set `FORGE_SANDBOX_MODE=host`, then
set `FORGE_ALLOW_HOST_EXECUTION=1` only when you accept host shell-execution risk.
The bundled Compose services deliberately do **not** mount the host Docker socket:
that socket is host-root equivalent. Run `forge` directly on the host for default
container-mode execution; a Compose-hosted Forge process fails closed instead of
receiving Docker-daemon authority.

That's it — the loop creates a default project, derives a contract, and works the goal
to completion (or pauses as `blocked` for you). Watch it live:

```bash
forge serve         # control-plane API on :8787
make gui            # React console (proxies /api -> :8787)
```

### Pure Docker (no local Python)

```bash
docker compose up -d                              # Postgres + control-plane API
docker compose run --rm forge forge run --goal "..."
```

Compose binds Postgres and the control plane to `127.0.0.1` and supplies a local
development control token. Set a strong `FORGE_CONTROL_TOKEN` before sharing the
machine or placing Forge behind another service.

## Configuration

Everything resolves from environment variables via [`forge_config.py`](forge_config.py);
see [`.env.example`](.env.example) for the full list. Highlights:

| Variable | Purpose | Default |
|---|---|---|
| `LLM_BASE_URL` / `LLM_MODEL` | model backend | `http://localhost:1234/v1`, a local Gemma |
| `PGE_<ROLE>_MODEL` | per-role model routing | falls back to `LLM_MODEL` |
| `DATABASE_URL` | engine Postgres | matches docker-compose |
| `FORGE_HOME` | state / logs / workspaces | `~/.forge` |
| `FORGE_DEFAULT_PROJECT` | pin a project | resolve-or-create |
| `FORGE_SANDBOX_MODE` | per-project workspace: container or host | `container` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | trace/metric collector | `http://localhost:4317` |

Run `forge config` to print the resolved configuration.

## Sandbox & observability

**Sandbox** ([`forge_runtime/sandbox.py`](forge_runtime/sandbox.py)): the default
workspace for every project is a long-lived, per-project Docker container —
`write_file`/`read_file`/`bash`/`install_deps`/`run_tests`/etc. all execute there,
not on your host. The container runs as a fixed non-root UID (`10001:10001`),
every Linux capability dropped, `no-new-privileges` set, and its files on a
Docker-managed named volume — never a host bind-mount, so it has no path back to
your filesystem at all. Host behavior is available only through the explicit
operator opt-in `FORGE_SANDBOX_MODE=host`. In the default
`container` mode, Docker or sandbox setup failure stops the run before a tool can
touch the host. A non-empty existing workspace is imported into its new named
volume once; later container results are copied back through a staged,
transactional replacement. Controlled by `FORGE_SANDBOX_MODE` (`container`
default / `host`) and `FORGE_SANDBOX_IMAGE`; see
[`docker/sandbox/Dockerfile`](docker/sandbox/Dockerfile) for the toolchain baked
into the default image (python/pytest/ruff, node/npm, cmake/build-essential/
clang-format, rustc/cargo, ripgrep/git/curl).

Host mode is for an operator-controlled local experiment. Generated-contract
strength and mutation verification deliberately require the container boundary,
so Forge defers completion instead of executing those probes on the host.

Four new tools ride on top of the same sandbox and close the gap where the
executor used to hand-assemble build/test/lint commands via raw shell: **`run_tests`**,
**`build`**, **`lint`** (`fix=true` to auto-fix), and **`audit_deps`** — each
auto-detects the stack (Cargo.toml/CMakeLists.txt/package.json/else python) and
runs the right OSS tool (cargo test/clippy/audit, cmake+ctest, npm test/audit,
pytest/ruff/pip-audit).

**Observability** ([`forge_runtime/telemetry.py`](forge_runtime/telemetry.py)):
OpenTelemetry traces every PGE node (planner/auditor/executor/evaluator) and tool
call, plus counters for tool invocations and completion-gate blocks (mutation/
vacuous-test/overfitting/scope-completeness). Exports via OTLP:

```bash
docker compose up -d otel-collector
docker compose logs -f otel-collector   # see spans/metrics as they land
```

Point the collector's exporters ([`otel-collector-config.yaml`](otel-collector-config.yaml))
at Jaeger/Grafana/etc. to get a proper trace UI. Degrades to silently running
without tracing if no collector is reachable — optional infra never blocks a run.

## How it works

```
planner ─► auditor ─► executor ─► evaluator ─► (planner | end)
              │           │            │
        dual contract  one bounded   RUNS the contract,
        (checklist +   action       verdict from exit codes
        test list)
```

The loop runs in **batches**, each checkpointed to Postgres so it resumes safely;
runs can be launched **detached** to outlive the shell or gateway that started them.
Full design: [ARCHITECTURE.md](ARCHITECTURE.md).

## Development

```bash
make test    # pytest incl. the regression suite
make fmt     # ruff lint/format
```

## Project layout

```
forge_config.py     # single source of truth for paths, DB, providers
forge_cli.py        # `forge` entry point (run / serve / config)
run_pge.py          # the batch loop (attached)
pge_launcher.py     # detached launcher + run manifest
engine/src/         # the PGE graph: planner, auditor, executor, evaluator, steward
forge_runtime/sandbox.py     # per-project container sandbox (default workspace)
forge_runtime/telemetry.py   # OpenTelemetry tracing/metrics
docker/sandbox/     # the sandbox container image (Dockerfile)
app/                # SQLAlchemy models + services (Postgres)
control_plane/      # FastAPI API behind the console
web/                # React 19 + Vite operator console
tests/regression/   # one test per hard-won failure mode
contracts/          # JSON schemas for run snapshots + provider profiles
```

## Roadmap

A Rust `forge` operator CLI (early code in `experimental/`), multi-tenant auth, and a
hosted deployment path. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

Launch messaging, demo scripts, social artwork, and community plans live in
[`docs/marketing/`](docs/marketing/README.md).

## License

[Apache-2.0](LICENSE).
