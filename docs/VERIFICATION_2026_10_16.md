# Verification record — 16 October 2026

Base source revision: `d2a900603f6de19267518eeabe6ebfdc006943a2`.
The preparation change adds an offline demo, walkthrough, contributor guidance,
and documentation corrections; it does not change the agent engine.

| Check | Result |
| --- | --- |
| Python 3.12.14, editable development install | Successful |
| `ruff check .` | Passed |
| `python examples/verification_demo.py` using base Python without installed Forge dependencies | Passed all four observations |
| `python -m pytest -o addopts= -q -ra` | 313 passed, 16 skipped |
| Relative links in changed Markdown files; `git diff --check` | Passed |

## Environment and scope

The first suite attempt lacked CMake and did not preserve the test virtual
environment in login-shell subprocesses. The environment also advertised a SOCKS
proxy without the optional SOCKS client dependency. After installing CMake and
`socksio` into the disposable test environment and preserving that environment's
PATH in child Bash shells, the full Python suite produced the result above.
No test assertion was removed or weakened to obtain it.

Six durable-Postgres integration tests, one live-search test, and nine Docker
tests were skipped under their existing opt-in conditions. The run does not
establish durable database recovery, live model behavior, Docker isolation,
frontend behavior, or Rust behavior. Those components were not changed here.

The offline demonstration exercises `auditor.harden_pipefail` directly with a
negative control, a positive control, and an idempotence check. It is not a
measurement of end-to-end coding-agent success or a model benchmark.

This record is a dated local observation. Use the PR checks for results on the
eventual committed revision and the CI Python-version matrix.
