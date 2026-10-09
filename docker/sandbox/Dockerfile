# Forge sandbox image: the default per-project workspace container
# (forge_runtime/sandbox.py). Bakes in the multi-stack toolchain observed
# across real goals this harness has run (python, node, C++/CMake, rust) plus
# ripgrep/git/curl for the executor's grep/git_diff/fetch_doc tools, so most
# goals need no extra install_deps round-trip just to get a compiler.
#
# The image itself has no notion of "the sandbox user" baked into its default
# CMD — forge_runtime/sandbox.py always runs the container as UID:GID
# 10001:10001 via `docker run --user`, with every capability dropped and
# no-new-privileges set. The uid 10001 user created below exists so file
# ownership inside the mounted volume is stable and human-readable
# (`ls -la` shows "sandbox", not a bare number) — it grants no privilege by
# itself; the run-time flags in sandbox.py are what actually remove root.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    nodejs npm \
    build-essential cmake clang-format \
    rustc cargo \
    ripgrep git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Baked in rather than installed lazily by run_tests/lint/audit_deps at tool-call
# time: (1) pytest missing meant the python run_tests fallback silently reported
# a false "OK" via `unittest discover` finding zero pytest-style tests — a real
# bug caught by smoke-testing this image before wiring it in; (2) Debian's
# python3-pip refuses any runtime `pip install` against the system interpreter
# (PEP 668 "externally managed environment") unless a project's own .venv is
# active, which run_tests/lint/audit_deps can't assume exists yet.
# --break-system-packages is safe here: this IS the disposable, non-root,
# capability-dropped sandbox environment, not a host Python install.
#
# numpy/scikit-learn are PINNED and baked in because the canonical suite's
# digits goal both trains and is independently VERIFIED against a reproduced
# accuracy > 0.95. If the verifier runtime lacked sklearn (as the July-13 host
# verifier did), digits could only ever return "unverifiable" — a missing
# dependency masquerading as an inconclusive result. Pinning also makes the
# reproduced accuracy deterministic and records a stable image digest per run.
RUN pip install --no-cache-dir --break-system-packages \
    pytest==8.3.4 ruff==0.8.6 pip-audit==2.7.3 \
    numpy==2.1.3 scikit-learn==1.6.0

RUN groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash sandbox

RUN mkdir -p /workspace && chown -R 10001:10001 /workspace
WORKDIR /workspace

# No USER directive here on purpose: forge_runtime/sandbox.py's bootstrap
# step runs a throwaway `docker run` AS ROOT once, against a fresh empty
# volume, purely to chown it to 10001:10001 (a brand-new Docker volume is
# root-owned by default). Every command hermes/the executor actually issues
# runs via `docker exec ... --user 10001:10001` against the LONG-LIVED
# container, which itself is also started with --user 10001:10001 — the
# image defaulting to root is what makes that one bootstrap chown possible;
# it is never the environment the executor's code runs in.
CMD ["sleep", "infinity"]
