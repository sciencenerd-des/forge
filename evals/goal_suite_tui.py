"""Run the canonical eight-goal PGE suite through the real launcher path.

Unlike goal_suite.py (which runs run_pge.py as a bare subprocess with no
run_id, so the launcher manifest and control plane never see it), this
driver creates a project + goal and calls pge_launcher.launch_pge exactly
like the control plane's `/runtime/runs/start` route does. Each goal is
therefore a durable, manifest-tracked run the Rust TUI's Operator view can
show live, polled from the same `runs.json` + control-plane API it always
reads.

Runs are sequential (one project at a time) to avoid contending for the
single local LM Studio model.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services import MemoryService
from evals.goal_suite import (
    GOALS,
    _build_report,
    _verdict,
    outcome_fields,
    reconcile_status,
)
from forge_config import DEFAULT_BASE_URL, ROLES
from pge_launcher import load_run_state, process_is_alive

POLL_SECONDS = 5


def configure_suite_provider(*, model: str, base_url: str) -> None:
    """Pin every PGE role to the provider selected by this evaluation.

    The launcher starts a child process, so merely accepting ``--model`` is
    insufficient: role-specific provider variables and the user's normal
    ``.env`` would otherwise select a different backend.  Keep this override
    process-local and inherited by the child; do not mutate provider files.
    """
    normalized_base_url = base_url.rstrip("/")
    if not normalized_base_url:
        raise ValueError("base_url must not be empty")

    os.environ["LLM_MODEL"] = model
    os.environ["FORGE_LLM_BASE_URL"] = normalized_base_url
    os.environ["FORGE_LLM_DIALECT"] = "openai"
    for role in ROLES:
        upper = role.upper()
        os.environ[f"PGE_{upper}_MODEL"] = model
        os.environ[f"FORGE_{upper}_BASE_URL"] = normalized_base_url


def _terminal(status: str) -> bool:
    return status in {"completed", "failed", "blocked", "stopped"}


def _consistent_terminal_status(manifest_status: str, goal_status: str | None) -> bool:
    """A launcher terminal state is evidence only when durable state agrees."""
    return _terminal(manifest_status) and goal_status == manifest_status


def run_suite(*, model: str, base_url: str, timeout: int, max_turns: int, output: Path,
              only: int | None = None) -> dict[str, Any]:
    started = time.monotonic()
    results = []
    selected = GOALS if only is None else [GOALS[only - 1]]
    configure_suite_provider(model=model, base_url=base_url)
    os.environ["PGE_MAX_TURNS"] = str(max_turns)
    for index, (slug, goal) in enumerate(selected, start=1):
        project_name = f"suite-{index}-{slug}-{uuid.uuid4().hex[:8]}"
        repo_path = str(Path.home() / ".forge" / "workspaces" / project_name)
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        with SessionLocal() as db:
            service = MemoryService(db)
            project = service.create_project(project_name, repo_path)
            project_id = project.id
            service.create_goal(project_id=project_id, title=goal, description=goal)

        from pge_launcher import launch_pge
        begin = time.monotonic()
        launch = launch_pge(project_id, source="eval-tui", invocation={"goal": goal})
        status, error = "failed", launch.get("message")
        if launch.get("status") == "success":
            deadline = begin + timeout
            while time.monotonic() < deadline:
                manifest = load_run_state().get(project_id, {})
                current_status = manifest.get("status", "")
                if _terminal(current_status):
                    status, error = current_status, manifest.get("failure")
                    break
                if not process_is_alive(manifest.get("pid")):
                    status, error = manifest.get("status", "failed"), manifest.get("failure")
                    break
                time.sleep(POLL_SECONDS)
            else:
                status, error = "timeout", f"exceeded {timeout}s"
        durable = _verdict(project_name, slug)
        if _terminal(status) and not _consistent_terminal_status(status, durable.get("goal_status")):
            status, error = (
                "inconsistent",
                f"launcher reported terminal state but durable goal is {durable.get('goal_status')!r}",
            )
        # The launcher's terminal "completed" is only a claim. The acceptance
        # contract's independent re-run is the arbiter: rejected -> false
        # completion; unverifiable/error on a claimed completion -> "unverified".
        status = reconcile_status(status, durable)
        if status in ("rejected", "unverified"):
            error = error or durable.get("acceptance_reason")
        outcome, false_completion = outcome_fields(status)
        results.append({"slug": slug, "goal": goal, "project": project_name,
                        "goal_id": slug, "outcome": outcome, "false_completion": false_completion,
                        "status": status, "elapsed_s": round(time.monotonic() - begin, 2),
                        "error": error, "turns_used": None, **durable})
        print(json.dumps(results[-1], indent=2), flush=True)
    report = _build_report(model, results, started)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="OpenAI-compatible base URL for every PGE role")
    parser.add_argument("--timeout", type=int, default=int(float(os.getenv("SUITE_GOAL_TIMEOUT_MINUTES", "30")) * 60))
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--output", type=Path, default=Path("evals/results/goal-suite-tui.json"))
    parser.add_argument("--only", type=int, choices=range(1, 9))
    args = parser.parse_args()
    report = run_suite(model=args.model, base_url=args.base_url, timeout=args.timeout,
                       max_turns=args.max_turns, output=args.output, only=args.only)
    print(json.dumps(report, indent=2))
    return 0 if report["completed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
