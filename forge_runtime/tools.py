from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_OUTPUT_BYTES = 64_000
MAX_READ_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 120


def host_execution_allowed() -> bool:
    """Allow arbitrary subprocesses only in isolation or by explicit opt-in."""
    configured = os.getenv("FORGE_ALLOW_HOST_EXECUTION", "").strip().lower()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return Path("/.dockerenv").exists()


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    allow_write: bool = True
    allow_shell: bool = True
    allow_network: bool = False
    allow_destructive: bool = False
    allowed_hosts: frozenset[str] = frozenset()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = MAX_OUTPUT_BYTES
    event_sink: Callable[[dict[str, Any]], None] | None = None

    def normalized(self) -> "ToolContext":
        return ToolContext(
            workspace=self.workspace.expanduser().resolve(strict=True),
            allow_write=self.allow_write,
            allow_shell=self.allow_shell,
            allow_network=self.allow_network,
            allow_destructive=self.allow_destructive,
            allowed_hosts=frozenset(host.lower() for host in self.allowed_hosts),
            timeout_seconds=max(1, min(self.timeout_seconds, 900)),
            max_output_bytes=max(1_024, min(self.max_output_bytes, 1_000_000)),
            event_sink=self.event_sink,
        )


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        if not name or name in self._handlers:
            raise ValueError(f"invalid or duplicate tool name: {name}")
        self._handlers[name] = handler

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def execute(self, context: ToolContext, request: ToolRequest) -> ToolResult:
        context = context.normalized()
        started = time.monotonic()
        handler = self._handlers.get(request.name)
        if handler is None:
            return ToolResult(False, request.name, error="unknown tool")
        try:
            result = handler(context, dict(request.arguments))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            result = ToolResult(False, request.name, error=str(error))
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if context.event_sink:
            context.event_sink({
                "type": "tool_invocation",
                "call_id": request.call_id,
                "request": {"name": request.name, "arguments": _redact(request.arguments)},
                "result": result.to_dict(),
            })
        return result


def _workspace_path(context: ToolContext, raw_path: Any, *, must_exist: bool = False) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path must be a non-empty string")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = context.workspace / candidate
    candidate = candidate.resolve(strict=must_exist)
    try:
        candidate.relative_to(context.workspace)
    except ValueError as error:
        raise ValueError("path escapes the configured workspace") from error
    return candidate


def _bounded_text(value: bytes | str, limit: int) -> tuple[str, bool]:
    raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    truncated = len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), truncated


def read_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path = _workspace_path(context, arguments.get("path"), must_exist=True)
    if not path.is_file():
        raise ValueError("path is not a regular file")
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"file exceeds {MAX_READ_BYTES} byte read limit")
    content, truncated = _bounded_text(path.read_bytes(), context.max_output_bytes)
    return ToolResult(True, "read_file", {"path": str(path), "content": content, "size": size}, truncated=truncated)


def write_file(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not context.allow_write:
        raise ValueError("write access is disabled")
    path = _workspace_path(context, arguments.get("path"))
    content = arguments.get("content")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if path.exists() and path.is_symlink():
        raise ValueError("refusing to write through a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.forge-tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    return ToolResult(True, "write_file", {"path": str(path), "bytes_written": len(content.encode())})


def list_files(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root = _workspace_path(context, arguments.get("path", "."), must_exist=True)
    limit = max(1, min(int(arguments.get("limit", 200)), 2_000))
    entries: list[str] = []
    iterator = root.rglob("*") if root.is_dir() else [root]
    for path in iterator:
        if len(entries) >= limit:
            break
        if any(part in {".git", "node_modules", "target", ".venv"} for part in path.parts):
            continue
        entries.append(str(path.relative_to(context.workspace)))
    return ToolResult(True, "list_files", {"entries": entries}, truncated=len(entries) == limit)


def search_text(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    query = arguments.get("query")
    if not isinstance(query, str) or not query or len(query) > 500:
        raise ValueError("query must contain 1-500 characters")
    root = _workspace_path(context, arguments.get("path", "."), must_exist=True)
    command = ["rg", "--json", "--max-count", "100", "--", query, str(root)]
    result = subprocess.run(command, cwd=context.workspace, capture_output=True, timeout=context.timeout_seconds)
    stdout, truncated = _bounded_text(result.stdout, context.max_output_bytes)
    stderr, stderr_truncated = _bounded_text(result.stderr, context.max_output_bytes)
    return ToolResult(result.returncode in (0, 1), "search_text", {"exit_code": result.returncode, "matches": stdout, "stderr": stderr}, truncated=truncated or stderr_truncated)


def run_command(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not context.allow_shell:
        raise ValueError("shell access is disabled")
    if not host_execution_allowed():
        raise ValueError(
            "host command execution is disabled; run Forge in a container or set "
            "FORGE_ALLOW_HOST_EXECUTION=1 after accepting host filesystem risk"
        )
    command = arguments.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("command must be a non-empty argv array or shell-like string")
    if command[0] in {"sudo", "su", "doas"}:
        raise ValueError("privilege escalation commands are not allowed")
    if not context.allow_destructive and _is_destructive_command(command):
        raise ValueError("destructive command requires explicit approval")
    timeout = max(1, min(int(arguments.get("timeout_seconds", context.timeout_seconds)), context.timeout_seconds))
    result = subprocess.run(command, cwd=context.workspace, capture_output=True, timeout=timeout, env=_safe_environment())
    stdout, stdout_truncated = _bounded_text(result.stdout, context.max_output_bytes)
    stderr, stderr_truncated = _bounded_text(result.stderr, context.max_output_bytes)
    return ToolResult(result.returncode == 0, "run_command", {"argv": command, "exit_code": result.returncode, "stdout": stdout, "stderr": stderr}, error=None if result.returncode == 0 else f"command exited with {result.returncode}", truncated=stdout_truncated or stderr_truncated)


def browser_fetch(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if not context.allow_network:
        raise ValueError("network access is disabled")
    raw_url = arguments.get("url")
    if not isinstance(raw_url, str):
        raise ValueError("url must be a string")
    url = urllib.parse.urlparse(raw_url)
    host = (url.hostname or "").lower()
    if url.scheme not in {"http", "https"} or not host:
        raise ValueError("browser URL must use HTTP or HTTPS")
    if host not in context.allowed_hosts:
        raise ValueError("browser host is not allowlisted")
    request = urllib.request.Request(raw_url, headers={"User-Agent": "ForgeHarness/0.1"})
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=context.timeout_seconds) as response:
            content, truncated = _bounded_text(response.read(context.max_output_bytes + 1), context.max_output_bytes)
            return ToolResult(True, "browser_fetch", {"url": response.url, "status": response.status, "content_type": response.headers.get("content-type", ""), "content": content}, truncated=truncated)
    except urllib.error.HTTPError as error:
        return ToolResult(False, "browser_fetch", {"url": raw_url, "status": error.code}, error=f"HTTP {error.code}")


def notebook_cell(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """Append a scratch cell and optionally execute it in a fresh Python process."""
    if not context.allow_write:
        raise ValueError("write access is disabled")
    path = _workspace_path(context, arguments.get("path", ".forge/notebooks/scratch.ipynb"))
    if path.suffix != ".ipynb":
        raise ValueError("notebook path must end with .ipynb")
    cell_type = arguments.get("cell_type", "code")
    if cell_type not in {"code", "markdown"}:
        raise ValueError("cell_type must be code or markdown")
    source = arguments.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if len(source.encode("utf-8")) > 100_000:
        raise ValueError("notebook cell exceeds 100000 bytes")
    execute = bool(arguments.get("execute", cell_type == "code"))
    if execute and cell_type != "code":
        raise ValueError("only code cells can be executed")
    if execute and not context.allow_shell:
        raise ValueError("code execution is disabled")
    if execute and not host_execution_allowed():
        raise ValueError(
            "host code execution is disabled; run Forge in a container or set "
            "FORGE_ALLOW_HOST_EXECUTION=1 after accepting host filesystem risk"
        )

    if path.exists():
        if path.is_symlink():
            raise ValueError("refusing to write through a symlink")
        try:
            notebook = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid notebook: {error}") from error
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
            raise ValueError("invalid notebook structure")
    else:
        notebook = {
            "cells": [],
            "metadata": {
                "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                "language_info": {"name": "python", "version": "3"},
                "forge": {"purpose": "task scratchpad; not completion evidence"},
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }

    output_text = ""
    exit_code = None
    outputs: list[dict[str, Any]] = []
    execution_count = None
    truncated = False
    if execute:
        timeout = max(1, min(int(arguments.get("timeout_seconds", 60)), context.timeout_seconds))
        result = subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-c", source],
            cwd=context.workspace,
            capture_output=True,
            timeout=timeout,
            env=_safe_environment(),
        )
        exit_code = result.returncode
        stdout, stdout_truncated = _bounded_text(result.stdout, context.max_output_bytes)
        stderr, stderr_truncated = _bounded_text(result.stderr, context.max_output_bytes)
        output_text = stdout + stderr
        if stdout:
            outputs.append({"name": "stdout", "output_type": "stream", "text": stdout.splitlines(True)})
        if stderr:
            outputs.append({"name": "stderr", "output_type": "stream", "text": stderr.splitlines(True)})
        execution_count = 1 + sum(1 for cell in notebook["cells"] if cell.get("cell_type") == "code")
        truncated = stdout_truncated or stderr_truncated

    cell = {
        "cell_type": cell_type,
        "metadata": {"forge": {"executed": execute, "exit_code": exit_code}},
        "source": source.splitlines(True),
    }
    if cell_type == "code":
        cell.update({"execution_count": execution_count, "outputs": outputs})
    notebook["cells"].append(cell)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.forge-tmp-{os.getpid()}")
    temporary.write_text(json.dumps(notebook, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return ToolResult(
        exit_code in (None, 0),
        "notebook_cell",
        {"path": str(path), "cell_index": len(notebook["cells"]) - 1,
         "executed": execute, "exit_code": exit_code, "output": output_text},
        error=None if exit_code in (None, 0) else f"python exited with {exit_code}",
        truncated=truncated,
    )


def _safe_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "USER"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _is_destructive_command(command: list[str]) -> bool:
    executable = Path(command[0]).name
    if executable in {"rm", "rmdir", "shred", "mkfs", "diskutil", "dd"}:
        return True
    if executable == "git" and len(command) > 1:
        return command[1] in {"clean", "reset"} or (command[1] == "checkout" and "--" in command)
    return False


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are disabled", headers, fp)


def _redact(value: Any) -> Any:
    secret_markers = ("key", "token", "secret", "password", "authorization")
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(marker in key.lower() for marker in secret_markers) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("read_file", read_file)
    registry.register("write_file", write_file)
    registry.register("list_files", list_files)
    registry.register("search_text", search_text)
    registry.register("run_command", run_command)
    registry.register("browser_fetch", browser_fetch)
    registry.register("notebook_cell", notebook_cell)
    return registry
