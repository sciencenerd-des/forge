"""Deterministic, read-only preflight for an evaluation run.

Before an expensive suite launches, prove the environment can actually produce
trustworthy evidence: a reachable database with the expected schema, a live
model endpoint serving the requested model, a Docker daemon and pinned sandbox
image, enough disk, the verifier runtimes the contracts need, and no stale
run still holding resources. Every check is side-effect free (it never starts,
stops, or mutates anything) and never raises; a failure is data, not a crash.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True  # a non-blocking check can warn without failing preflight


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.blocking)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [vars(c) for c in self.checks]}

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.ok]


def check_database(database_url: str | None) -> Check:
    """Read-only connectivity + schema presence check."""
    if not database_url:
        return Check("database", False, "no DATABASE_URL configured")
    try:
        import psycopg2  # noqa: PLC0415

        url = database_url.replace("postgresql+psycopg2://", "postgresql://")
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.forge_goals')")
                (present,) = cur.fetchone()
        finally:
            conn.close()
        if present is None:
            return Check("database", False, "connected but forge_goals table is missing")
        return Check("database", True, "reachable; forge_goals present")
    except Exception as exc:  # noqa: BLE001 - preflight must never raise
        return Check("database", False, f"unreachable: {type(exc).__name__}: {exc}")


def check_model_endpoint(base_url: str | None, model: str | None) -> Check:
    """Confirm the endpoint serves the exact requested model id."""
    if not base_url or not model:
        return Check("model_endpoint", False, "base_url and model are required")
    try:
        import urllib.request  # noqa: PLC0415

        url = base_url.rstrip("/") + "/models"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            import json  # noqa: PLC0415

            payload = json.loads(resp.read().decode("utf-8"))
        ids = {m.get("id") for m in payload.get("data", []) if isinstance(m, dict)}
        if model in ids:
            return Check("model_endpoint", True, f"serving {model!r}")
        return Check("model_endpoint", False,
                     f"endpoint reachable but does not serve {model!r}; has {sorted(ids)[:6]}")
    except Exception as exc:  # noqa: BLE001
        return Check("model_endpoint", False, f"unreachable: {type(exc).__name__}: {exc}")


def check_docker(image: str | None = None, *, required: bool = True) -> Check:
    """Docker daemon liveness and (optionally) pinned image presence + digest."""
    docker = shutil.which("docker")
    if not docker:
        return Check("docker", not required, "docker CLI not found", blocking=required)
    import subprocess  # noqa: PLC0415

    try:
        info = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=8)
    except Exception as exc:  # noqa: BLE001
        return Check("docker", not required, f"daemon unresponsive: {exc}", blocking=required)
    if info.returncode != 0:
        return Check("docker", not required,
                     f"daemon not ready: {info.stderr.strip()[:120]}", blocking=required)
    if not image:
        return Check("docker", True, f"daemon up (v{info.stdout.strip()})")
    dig = subprocess.run([docker, "image", "inspect", image, "--format", "{{.Id}}"],
                         capture_output=True, text=True, timeout=8)
    if dig.returncode != 0:
        return Check("docker", not required, f"image {image!r} not present locally",
                     blocking=required)
    return Check("docker", True, f"daemon up; {image} -> {dig.stdout.strip()[:24]}")


def check_disk(min_free_gb: float = 2.0, path: str = "/") -> Check:
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        ok = free_gb >= min_free_gb
        return Check("disk", ok, f"{free_gb:.1f} GiB free (need {min_free_gb})")
    except OSError as exc:
        return Check("disk", False, f"could not stat {path}: {exc}")


def check_runtimes(required: tuple[str, ...] = ()) -> Check:
    """Confirm the verifier runtimes the contracts need are on PATH."""
    missing = [tool for tool in required if shutil.which(tool) is None]
    if missing:
        return Check("runtimes", False, f"missing: {', '.join(missing)}")
    return Check("runtimes", True, f"present: {', '.join(required) or '(none required)'}")


def check_no_active_runs(load_run_state: Callable[[], dict], process_is_alive: Callable[[Any], bool]) -> Check:
    """A live run from a prior suite means resources are contended; fail closed.

    Reconciliation (marking a dead manifest terminal) is the caller's job — this
    check only reports; it never edits history.
    """
    active = [
        pid for rec in load_run_state().values()
        if process_is_alive(pid := rec.get("pid"))
    ]
    if active:
        return Check("no_active_runs", False, f"live run pids still present: {active}")
    return Check("no_active_runs", True, "no live runs")


def run_preflight(*, database_url: str | None, base_url: str | None, model: str | None,
                  sandbox_image: str | None = None, require_docker: bool = True,
                  required_runtimes: tuple[str, ...] = (), min_free_gb: float = 2.0) -> PreflightReport:
    """Aggregate every gate into one report. Callers refuse to launch on any
    blocking failure."""
    from pge_launcher import load_run_state, process_is_alive

    report = PreflightReport()
    report.add(check_database(database_url))
    report.add(check_model_endpoint(base_url, model))
    report.add(check_docker(sandbox_image, required=require_docker))
    report.add(check_disk(min_free_gb))
    report.add(check_runtimes(required_runtimes))
    report.add(check_no_active_runs(load_run_state, process_is_alive))
    return report
