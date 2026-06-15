# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Initial open-source extraction of the Forge autonomy harness.
- `forge_config.py` — single source of truth for paths, databases, the default
  project (resolve-or-create), and generic OpenAI-compatible provider profiles.
- `forge` CLI (`run` / `serve` / `config`) and packaging via `pyproject.toml`.
- Docker + docker-compose stack (Postgres + control plane) for zero-setup runs.
- Regression suite (`tests/regression/`) — one test per historical failure mode:
  the audit-test quality gate, render-variety contract, loop resilience,
  run_command shell routing, planner-router edge mapping, launcher lifecycle.
- `ARCHITECTURE.md` and `docs/LESSONS.md`.
- CI (lint + tests + web build) on Python 3.11/3.12.

### Engine reliability (carried over from the hardened harness)
- The detached loop survives node-level exceptions, not just transient ones —
  one bad batch costs a retry, not the run.
- The render contract's variety check is satisfiable for P3 and P6 and is no
  longer silently dropped by the quality gate (the "loop never persists" bug).
- `write_file` overwrite of a small file the model intends to replace; shell-shaped
  `run_command` routed through bash; stagnation re-decomposes instead of crashing.
