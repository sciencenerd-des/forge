# Contributing to Forge

Thanks for your interest! Forge is a young project and contributions are welcome.

## Getting set up

```bash
make setup      # venv + editable install + .env
make db         # Postgres via docker
make test       # run the suite
make fmt        # ruff lint/format
```

## Ground rules

- **Keep the invariants in [docs/LESSONS.md](docs/LESSONS.md) true.** Each one exists
  because a real autonomous run broke. If your change touches the loop, the contract
  gate, the routers, or the launcher, make sure the matching `tests/regression/` test
  still passes — and add a new one if you fix a new failure mode.
- **Nothing machine-specific in the engine.** New paths, DBs, ports, or model settings
  go through [`forge_config.py`](forge_config.py) and `.env.example`, never hardcoded.
- **Acceptance tests must be satisfiable and shell-based** where possible (see lessons
  #1 and #2). A contract check that can't pass — or that the quality gate silently
  drops — is worse than no check.
- Run `make fmt` and `make test` before opening a PR.

## Pull requests

1. Fork, branch from `main`.
2. Make focused changes with a clear description of the problem and the fix.
3. Add/adjust tests; update docs if behavior changes.
4. Ensure CI is green.

## Reporting bugs

Open an issue with: what you ran (`forge ...`), your backend (`LLM_BASE_URL`/model),
the relevant log from `~/.forge/logs/pge_runs/`, and what you expected.
