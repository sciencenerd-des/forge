"""Exercise Forge's real pipeline hardening without a model, database, or Docker.

Run from a source checkout: python examples/verification_demo.py
Only the fixed commands below execute, inside an automatically removed temp folder.
This demonstrates one verification primitive, not an autonomous coding run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine")]

from src.auditor import harden_pipefail  # noqa: E402


def run(command: str, workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", command],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def main() -> int:
    if shutil.which("bash") is None or shutil.which("tail") is None:
        print("This demo requires bash and tail (macOS, Linux, or WSL).", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="forge-verification-demo-") as folder:
        workspace = Path(folder)
        # A producer fails, but the final pipeline stage succeeds.
        contract = [{"id": "demo", "command": "(echo BROKEN; exit 1) | tail -1"}]
        unsafe = run(contract[0]["command"], workspace)
        hardened, repaired = harden_pipefail(contract)
        safe = run(hardened[0]["command"], workspace)
        if repaired != ["demo"] or unsafe.returncode != 0 or safe.returncode != 1:
            raise RuntimeError("Expected the original false pass and the hardened failure")

        print("Forge verification demo - actual auditor.harden_pipefail")
        print(f"1. Broken producer, ordinary pipeline: exit={unsafe.returncode} (FALSE PASS)")
        print(f"2. Same producer, hardened pipeline:  exit={safe.returncode} (FAILURE PRESERVED)")
        print(f"   command: {hardened[0]['command']}")

        # A positive control ensures hardening still accepts a working producer.
        good, _ = harden_pipefail(
            [{"id": "positive", "command": "(echo CORRECT; exit 0) | tail -1"}]
        )
        positive = run(good[0]["command"], workspace)
        if positive.returncode != 0 or positive.stdout.strip() != "CORRECT":
            raise RuntimeError("A genuinely successful pipeline must still pass")
        again, changed = harden_pipefail(hardened)
        if again != hardened or changed:
            raise RuntimeError("Hardening an already hardened command must be idempotent")
        print(f"3. Working producer, hardened pipeline: exit={positive.returncode} (REAL PASS)")
        print("4. Reapplying hardening: unchanged (IDEMPOTENT)")
        print("PASS: negative and positive controls behaved as expected.")
        print("Scope: pipeline exit status only; no model, sandbox, or durable run was tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
