# Working on Forge

Forge is a coding-agent reliability harness. Read `README.md`,
`docs/LESSONS.md`, and the relevant regression test before changing behavior.

## Setup and checks

- Python 3.11–3.14: `python3 -m venv .venv`, then
  `.venv/bin/python -m pip install -e ".[dev]"`.
- Fast, dependency-free introduction: `python3 examples/verification_demo.py`.
- Python checks: `.venv/bin/ruff check .` and `.venv/bin/pytest -q`.
- Web checks, when changing `web/`: `cd web && npm ci && npm run lint && npm run build`.
- Rust checks, when changing `experimental/`: `cd experimental && cargo test --workspace`.
- Record skips and untested services. A unit-test pass does not establish that
  Docker, Postgres, or a live model works.

## Invariants

- Preserve acceptance-contract coverage; never delete a failing test to get green.
- Keep `blocked`, `complete_unverified`, and `verified` distinct. Do not turn a
  failed or unavailable review into a successful verdict.
- Preserve exit codes, useful diagnostics, sandbox boundaries, and credential masking.
- Keep generated project work outside the Forge source tree and operator home.
- Add a focused regression for a behavior fix, including a negative control where useful.
- Use synthetic data in demos. Do not commit tokens, private transcripts, or `.env` files.

## Explaining results

Give the command, observed result, and scope of verification. Separate measured
behavior from intended architecture and untested claims. Identify whether Codex
assisted development, ran as a subprocess, or supplied model inference; those are
different roles. Do not imply endorsement by a provider.
