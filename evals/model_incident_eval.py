from __future__ import annotations

import argparse
import json
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ALLOWED_TOOLS = {"read_file", "write_file", "run_command", "notebook_cell"}

FIXTURES = {
    "valid_structured_output": """Return exactly one executor JSON object. Inspect src/app.py before changing it.
Allowed shape: {"type":"tool_call","name":"read_file","arguments":{"path":"src/app.py"}}.
No markdown or prose.""",
    "correct_tool_selection": """A failing assertion points at src/parser.py line 41, but the file contents are unknown.
Choose exactly one next tool action. Inspect the relevant source before editing. Return executor JSON only.""",
    "no_repeated_action": """Previous action already completed successfully:
read_file({"path":"src/parser.py"}) -> {"ok":true,"data":{"content":"def parse(value): ..."}}
Do not repeat the same action. The next step is to inspect the focused regression test at tests/test_parser.py.
Return exactly one executor JSON action.""",
    "tool_result_interpretation": """The last tool result was:
write_file({"path":"src/parser.py","content":"fixed implementation"}) ->
{"ok":true,"tool":"write_file","data":{"bytes_written":20},"error":null}
Interpret ok=true as success. Choose the next action to verify the focused parser test using run_command.
Return executor JSON only.""",
    "failed_command_recovery": """The last action failed:
run_command({"argv":["python3","-m","pytest","-q"]}) ->
{"ok":false,"data":{"exit_code":4,"stderr":"ERROR: file or directory not found: tests"},"error":"command exited with 4"}
Do not repeat that command and do not claim completion. Choose one diagnostic read action to discover the repository's test configuration. Return executor JSON only.""",
}


@dataclass(frozen=True)
class FixtureResult:
    fixture: str
    passed: bool
    reason: str
    response: str
    latency_ms: int = 0


def _parse_action(raw: str) -> tuple[dict[str, Any] | None, str]:
    try:
        value = json.loads(raw.strip())
    except (json.JSONDecodeError, TypeError) as error:
        return None, f"invalid JSON: {error}"
    if not isinstance(value, dict):
        return None, "response is not a JSON object"
    if value.get("type") == "tool_call":
        if value.get("name") not in ALLOWED_TOOLS:
            return None, "unknown executor tool"
        if not isinstance(value.get("arguments"), dict):
            return None, "tool_call arguments must be an object"
    elif value.get("type") == "heartbeat":
        required = {"progress_summary", "next_task_description", "blocker", "resume_instruction"}
        if not required.issubset(value):
            return None, "heartbeat is missing required fields"
    else:
        return None, "type must be tool_call or heartbeat"
    return value, "valid executor action"


def evaluate_response(fixture: str, raw: str) -> FixtureResult:
    action, reason = _parse_action(raw)
    if action is None:
        return FixtureResult(fixture, False, reason, raw)
    name = action.get("name")
    arguments = action.get("arguments", {})
    passed = False
    if fixture == "valid_structured_output":
        passed = name == "read_file" and arguments.get("path") == "src/app.py"
        reason = "valid, exact executor action" if passed else "expected read_file for src/app.py"
    elif fixture == "correct_tool_selection":
        passed = name == "read_file" and arguments.get("path") == "src/parser.py"
        reason = "inspects source before editing" if passed else "expected read_file for src/parser.py"
    elif fixture == "no_repeated_action":
        passed = not (name == "read_file" and arguments.get("path") == "src/parser.py")
        passed = passed and name == "read_file" and arguments.get("path") == "tests/test_parser.py"
        reason = "selected distinct requested evidence" if passed else "repeated prior action or ignored next evidence"
    elif fixture == "tool_result_interpretation":
        argv = arguments.get("argv") or arguments.get("command")
        command = " ".join(argv) if isinstance(argv, list) else str(argv or "")
        passed = name == "run_command" and "pytest" in command and "test_parser" in command
        reason = "treated ok=true write as success and moved to verification" if passed else "did not verify after successful write"
    elif fixture == "failed_command_recovery":
        passed = name == "read_file" and arguments.get("path") in {"pyproject.toml", "package.json", "README.md"}
        reason = "diagnosed test configuration after failure" if passed else "repeated failure or skipped diagnosis"
    else:
        raise ValueError(f"unknown fixture: {fixture}")
    return FixtureResult(fixture, passed, reason, raw)


def query_model(base_url: str, model: str, prompt: str, timeout: int) -> tuple[str, int]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 500,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "executor_action",
                "strict": False,
                "schema": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["tool_call", "heartbeat"]},
                        "name": {"type": "string", "enum": sorted(ALLOWED_TOOLS)},
                        "arguments": {"type": "object"},
                        "progress_summary": {"type": "string"},
                        "next_task_description": {"type": "string"},
                        "blocker": {"type": ["string", "null"]},
                        "resume_instruction": {"type": "string"},
                    },
                    "required": ["type"],
                },
            },
        },
        "extra_body": {"reasoning_effort": "none"},
    }).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer not-needed"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    latency_ms = int((time.monotonic() - started) * 1000)
    return payload["choices"][0]["message"]["content"] or "", latency_ms


def run_evaluation(base_url: str, model: str, timeout: int) -> dict[str, Any]:
    results = []
    for fixture, prompt in FIXTURES.items():
        try:
            raw, latency_ms = query_model(base_url, model, prompt, timeout)
            result = evaluate_response(fixture, raw)
            results.append(FixtureResult(**{**asdict(result), "latency_ms": latency_ms}))
        except Exception as error:
            results.append(FixtureResult(fixture, False, f"model call failed: {error}", ""))
    passed = sum(result.passed for result in results)
    return {
        "model": model,
        "score": passed,
        "total": len(results),
        "pass_rate": passed / len(results),
        "results": [asdict(result) for result in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a local model against deterministic Forge incidents.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_evaluation(args.base_url, args.model, args.timeout)
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["score"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
