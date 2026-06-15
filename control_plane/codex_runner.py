from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .models import RunRecord
from .service import append_event, now_utc


def run_codex_goal(
    db: Session,
    run_id: str,
    workspace: Path,
    prompt: str,
    *,
    model: str = "gpt-5.5",
    reasoning_effort: str = "low",
    verification_contract: Path | None = None,
) -> int:
    run = db.get(RunRecord, run_id)
    if not run:
        raise ValueError("run not found")
    workspace = workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")

    _set_stage(db, run_id, "executor", "codex.started", {"model": model, "reasoning_effort": reasoning_effort, "workspace": str(workspace)})
    exit_code = _stream_codex(db, run_id, _codex_command(workspace, prompt, model, reasoning_effort, sandbox="workspace-write"), "codex")
    if exit_code != 0:
        return _finish(db, run_id, "failed", "executor", f"codex_exec_exit_{exit_code}", exit_code)

    contract_path = (verification_contract or workspace / "verification_contract.json").resolve()
    _set_stage(db, run_id, "evaluator", "evaluator.started", {"contract": str(contract_path)})
    try:
        contract = _load_contract(contract_path, workspace)
        evaluation = _evaluate_contract(contract, workspace)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        evaluation = {"passed": False, "error": str(error)}
    append_event(db, run_id, "evaluator.finished", "deterministic-evaluator", _bounded_payload(evaluation))
    db.commit()
    if not evaluation["passed"]:
        return _finish(db, run_id, "failed", "evaluator", "evaluation_failed", 2)

    _set_stage(db, run_id, "auditor", "auditor.started", {"images": contract.get("images", [])})
    audit = _run_auditor(db, run_id, workspace, prompt, contract, evaluation, model, reasoning_effort)
    append_event(db, run_id, "auditor.finished", "codex-auditor", _bounded_payload(audit))
    db.commit()
    if not audit.get("passed"):
        return _finish(db, run_id, "failed", "auditor", "audit_failed", 3)
    return _finish(db, run_id, "completed", "auditor", "verified_and_audited", 0)


def _codex_command(workspace: Path, prompt: str, model: str, reasoning_effort: str, *, sandbox: str) -> list[str]:
    return [
        "codex", "exec", "--model", model, "--config", f'model_reasoning_effort="{reasoning_effort}"',
        "--sandbox", sandbox, "--skip-git-repo-check", "--json", "--cd", str(workspace), prompt,
    ]


def _stream_codex(db: Session, run_id: str, command: list[str], actor: str) -> int:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=_codex_environment())
    assert process.stdout is not None
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"message": line[:4_000]}
        append_event(db, run_id, str(payload.get("type", f"{actor}.output"))[:64], actor, _bounded_payload(payload))
        db.commit()
    return process.wait()


def _load_contract(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing verification contract: {path}")
    contract = json.loads(path.read_text())
    if not isinstance(contract, dict) or not isinstance(contract.get("commands"), list) or not contract["commands"]:
        raise ValueError("verification contract requires a non-empty commands array")
    for key in ("artifacts", "images"):
        if not isinstance(contract.get(key, []), list):
            raise ValueError(f"verification contract {key} must be an array")
        for value in contract.get(key, []):
            candidate = (workspace / str(value)).resolve()
            if not candidate.is_relative_to(workspace):
                raise ValueError(f"{key} path escapes workspace: {value}")
    return contract


def _evaluate_contract(contract: dict[str, Any], workspace: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    passed = True
    for command in contract["commands"]:
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError("each verification command must be a non-empty argv array")
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=900, env=_codex_environment())
        item = {"command": command, "exit_code": result.returncode, "output": (result.stdout + result.stderr)[-8_000:]}
        results.append(item)
        passed = passed and result.returncode == 0
    missing = [path for path in contract.get("artifacts", []) + contract.get("images", []) if not (workspace / path).is_file()]
    return {"passed": passed and not missing, "commands": results, "missing": missing}


def _run_auditor(db: Session, run_id: str, workspace: Path, goal: str, contract: dict[str, Any], evaluation: dict[str, Any], model: str, reasoning_effort: str) -> dict[str, Any]:
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"passed": {"type": "boolean"}, "summary": {"type": "string"}, "failures": {"type": "array", "items": {"type": "string"}}},
        "required": ["passed", "summary", "failures"],
    }
    with tempfile.TemporaryDirectory(prefix="forge-audit-") as temp:
        schema_path = Path(temp) / "schema.json"
        output_path = Path(temp) / "verdict.json"
        schema_path.write_text(json.dumps(schema))
        prompt = (
            "You are the independent final auditor. Do not edit files. Audit the completed goal against the repository, "
            "verification evidence, and attached render images. Reject missing, visibly broken, misleading, or incomplete work.\n"
            f"GOAL:\n{goal}\nCONTRACT:\n{json.dumps(contract)}\nEVALUATION:\n{json.dumps(evaluation)}"
        )
        command = _codex_command(workspace, prompt, model, reasoning_effort, sandbox="read-only")
        for image in contract.get("images", []):
            command[2:2] = ["--image", str((workspace / image).resolve())]
        command[2:2] = ["--output-schema", str(schema_path), "--output-last-message", str(output_path)]
        exit_code = _stream_codex(db, run_id, command, "auditor")
        if exit_code != 0 or not output_path.is_file():
            return {"passed": False, "summary": f"auditor exited {exit_code}", "failures": ["auditor did not produce a verdict"]}
        try:
            verdict = json.loads(output_path.read_text())
        except json.JSONDecodeError as error:
            return {"passed": False, "summary": "malformed auditor verdict", "failures": [str(error)]}
        return verdict if isinstance(verdict, dict) else {"passed": False, "summary": "invalid auditor verdict", "failures": []}


def _set_stage(db: Session, run_id: str, node: str, event_type: str, payload: dict[str, Any]) -> None:
    run = db.get(RunRecord, run_id)
    run.status = "running"
    run.current_node = node
    run.updated_at = now_utc()
    append_event(db, run_id, event_type, "codex-runner", payload)
    db.commit()


def _finish(db: Session, run_id: str, status: str, node: str, reason: str, exit_code: int) -> int:
    run = db.get(RunRecord, run_id)
    run.status = status
    run.current_node = node
    run.terminal_reason = reason
    run.updated_at = now_utc()
    append_event(db, run_id, "run.finished", "codex-runner", {"exit_code": exit_code, "status": status, "terminal_reason": reason})
    db.commit()
    return exit_code


def _codex_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "USER", "CODEX_HOME"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _bounded_payload(payload: dict) -> dict:
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= 32_000:
        return payload
    return {"type": payload.get("type"), "truncated": True, "preview": encoded[:32_000]}
