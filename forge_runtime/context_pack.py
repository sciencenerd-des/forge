"""Deterministic repository context packs for Forge projects.

This is Forge's repo-context layer: it turns a project workspace into a small,
cached, auditable summary that can be injected alongside durable memory. It is
separate from LLM/provider prompt caching and never relies on model judgement.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

import forge_config

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 512_000
DEFAULT_MAX_FILES = 16
MAX_CANDIDATE_FILES = 600

PROJECT_FILES = (
    ".forge.yaml",
    "AGENTS.md",
    "agents.md",
    "README.md",
    "readme.md",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "tsconfig.json",
    "next.config.js",
    "next.config.mjs",
    "vite.config.ts",
    "vite.config.js",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "Dockerfile",
    "docker-compose.yml",
)

ENTRYPOINT_FILES = (
    "src/app/page.tsx",
    "src/app/layout.tsx",
    "app/page.tsx",
    "pages/index.tsx",
    "src/main.tsx",
    "src/App.tsx",
    "main.py",
    "app.py",
    "src/main.py",
    "src/app.py",
)


@dataclass
class ContextFileSummary:
    path: str
    kind: str
    sha256: str
    size_bytes: int
    summary: str
    symbols: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)


@dataclass
class RepoContextPack:
    schema_version: int
    repo_root: str
    repo_fingerprint: str
    task_type: str
    selected_files: list[ContextFileSummary]
    task_focus_terms: list[str]
    steering: dict[str, Any]
    last_validation_command: str | None
    invalidation_rules: list[str]
    cache_status: str
    created_at: float
    updated_at: float


def build_repo_context_pack(
    repo_path: str | os.PathLike[str] | None,
    *,
    task_text: str = "",
    max_files: int = DEFAULT_MAX_FILES,
) -> dict[str, Any]:
    """Return a cached deterministic repo context pack.

    Missing or non-directory workspaces produce a bounded disabled pack instead
    of raising. Context generation should never block the agent loop.
    """
    if not repo_path:
        return _disabled_pack("project has no repo_path")

    repo_root = Path(repo_path).expanduser()
    if not repo_root.exists() or not repo_root.is_dir():
        return _disabled_pack(f"repo_path is not an existing directory: {repo_root}")

    repo_root = repo_root.resolve()
    task_type = _infer_task_type(repo_root, task_text)
    task_terms = _task_terms(task_text)
    selected = _select_files(repo_root, task_terms=task_terms, max_files=max(1, min(max_files, 64)))
    fingerprint = _fingerprint(repo_root, selected)
    cache_path = _cache_path(repo_root, task_type)

    cached = _read_cache(cache_path)
    if (
        cached
        and cached.get("schema_version") == SCHEMA_VERSION
        and cached.get("repo_fingerprint") == fingerprint
        and cached.get("task_type") == task_type
    ):
        cached["cache_status"] = "hit"
        return cached

    now = time.time()
    pack = RepoContextPack(
        schema_version=SCHEMA_VERSION,
        repo_root=str(repo_root),
        repo_fingerprint=fingerprint,
        task_type=task_type,
        selected_files=[_summarize_file(path, repo_root) for path in selected],
        task_focus_terms=task_terms[:16],
        steering=_build_steering(repo_root, task_type, task_text, selected),
        last_validation_command=_infer_validation_command(repo_root),
        invalidation_rules=[
            "Rebuild when git HEAD changes.",
            "Rebuild when git working-tree status changes.",
            "Rebuild when any selected file content hash changes.",
            "Rebuild when task type or context-pack schema version changes.",
        ],
        cache_status="rebuilt",
        created_at=float(cached.get("created_at", now)) if cached else now,
        updated_at=now,
    )
    data = asdict(pack)
    _write_cache(cache_path, data)
    return data


def _disabled_pack(reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "cache_status": "disabled",
        "reason": reason,
        "selected_files": [],
        "invalidation_rules": [],
    }


def _select_files(repo_root: Path, *, task_terms: list[str], max_files: int) -> list[Path]:
    out: list[Path] = []
    for rel in (*PROJECT_FILES, *ENTRYPOINT_FILES):
        path = repo_root / rel
        if path.is_file() and path not in out:
            out.append(path)
        if len(out) >= max_files:
            break
    if len(out) < max_files and task_terms:
        for path in _rank_task_relevant_files(repo_root, task_terms):
            if path not in out:
                out.append(path)
            if len(out) >= max_files:
                break
    return out


def _rank_task_relevant_files(repo_root: Path, task_terms: list[str]) -> list[Path]:
    candidates: list[tuple[int, str, Path]] = []
    for path in _iter_source_files(repo_root):
        rel = _rel(path, repo_root)
        haystack = f"{rel}\n{_read_text(path)[:40_000]}".lower()
        score = sum(haystack.count(term) for term in task_terms)
        if score:
            candidates.append((-score, rel, path))
    return [path for _, _, path in sorted(candidates)]


def _iter_source_files(repo_root: Path) -> list[Path]:
    ignored = {
        ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "dist", "build",
        ".next", ".turbo", ".pytest_cache", "__pycache__", ".mypy_cache",
    }
    allowed_suffixes = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".toml", ".md",
        ".yaml", ".yml", ".sql", ".css",
    }
    found: list[Path] = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [name for name in dirs if name not in ignored and not name.startswith(".cache")]
        base = Path(root)
        for name in files:
            path = base / name
            if path.suffix.lower() not in allowed_suffixes and name not in PROJECT_FILES:
                continue
            try:
                if path.stat().st_size <= MAX_FILE_BYTES:
                    found.append(path)
            except OSError:
                continue
            if len(found) >= MAX_CANDIDATE_FILES:
                return found
    return found


def _task_terms(task_text: str) -> list[str]:
    stop_words = {
        "a", "an", "and", "are", "as", "be", "build", "by", "for", "from",
        "goal", "in", "into", "is", "it", "make", "of", "on", "or", "task",
        "that", "the", "this", "to", "with", "work",
    }
    terms = []
    for token in re.findall(r"[A-Za-z0-9_./-]+", task_text.lower()):
        token = token.strip("./-")
        if len(token) < 3 or token in stop_words:
            continue
        terms.append(token)
    return sorted(set(terms), key=lambda item: (-len(item), item))[:32]


def _build_steering(repo_root: Path, task_type: str, task_text: str, selected: list[Path]) -> dict[str, Any]:
    validation = _infer_validation_command(repo_root)
    selected_rels = [_rel(path, repo_root) for path in selected]
    risks: list[str] = []
    next_actions: list[str] = []
    if not validation:
        risks.append("No deterministic validation command was inferred from project metadata.")
        next_actions.append("Inspect project scripts or tests and establish the smallest validation command before claiming completion.")
    else:
        next_actions.append(f"Run `{validation}` after the smallest implementation change.")
    if task_type == "debugging":
        next_actions.insert(0, "Reproduce or inspect the failing behavior before editing.")
    elif task_type == "feature":
        next_actions.insert(0, "Locate the narrow existing component or module boundary before adding new files.")
    elif task_type == "review":
        next_actions.insert(0, "Ground every finding in a concrete file, line, command, or artifact.")
    if not selected_rels:
        risks.append("No project files were selected for context; repo_path may be empty or unsupported.")
    return {
        "objective": task_text[:600],
        "mode": task_type,
        "selected_paths": selected_rels,
        "next_actions": next_actions[:5],
        "completion_gate": "Do not mark the task complete until acceptance criteria and deterministic validation pass.",
        "avoid": [
            "Do not rewrite unrelated files.",
            "Do not treat model confidence or heartbeat text as verification evidence.",
            "Do not broaden scope when a failing check points at a narrower module.",
        ],
        "risks": risks,
    }


def _fingerprint(repo_root: Path, selected: list[Path]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo_root": str(repo_root),
        "git_head": _git(repo_root, ["rev-parse", "HEAD"]),
        "git_status_hash": _sha_text(_git(repo_root, ["status", "--porcelain=v1"])),
        "files": [
            {
                "path": _rel(path, repo_root),
                "sha256": _sha_file(path),
                "size": path.stat().st_size,
            }
            for path in selected
        ],
    }
    return _sha_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _infer_task_type(repo_root: Path, task_text: str) -> str:
    text = (task_text or "").lower()
    if any(word in text for word in ("debug", "bug", "fix", "crash", "error", "failing")):
        return "debugging"
    if any(word in text for word in ("review", "audit", "security")):
        return "review"
    if any(word in text for word in ("feature", "implement", "build", "add", "improve", "enhance")):
        return "feature"
    if (repo_root / "package.json").exists():
        return "web"
    if (repo_root / "pyproject.toml").exists() or (repo_root / "requirements.txt").exists():
        return "python"
    return "general"


def _summarize_file(path: Path, repo_root: Path) -> ContextFileSummary:
    text = _read_text(path)
    symbols: list[str] = []
    contracts: list[str] = []
    summary = "No text content"

    try:
        if path.name == "package.json":
            summary, symbols, contracts = _summarize_package_json(text)
        elif path.name == "pyproject.toml":
            summary, symbols, contracts = _summarize_pyproject(text)
        elif path.suffix.lower() == ".py":
            summary, symbols, contracts = _summarize_python(text)
        elif path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            summary, symbols, contracts = _summarize_javascript_like(text)
        elif path.suffix.lower() in {".md", ".mdc"}:
            headings = [ln.lstrip("#").strip() for ln in text.splitlines() if ln.strip().startswith("#")]
            summary = f"Markdown guidance with headings: {', '.join(headings[:6])}" if headings else "Markdown guidance"
            symbols = headings[:16]
        elif path.name.startswith("requirements"):
            deps = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
            summary = f"{len(deps)} Python requirement entries"
            symbols = deps[:16]
        else:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            summary = lines[0][:180] if lines else "No text content"
    except Exception:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        summary = lines[0][:180] if lines else "No text content"

    return ContextFileSummary(
        path=_rel(path, repo_root),
        kind=_kind(path),
        sha256=_sha_file(path),
        size_bytes=path.stat().st_size,
        summary=summary,
        symbols=symbols[:24],
        contracts=contracts[:24],
    )


def _infer_validation_command(repo_root: Path) -> str | None:
    package_json = repo_root / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(_read_text(package_json)).get("scripts", {})
            if isinstance(scripts, dict):
                for name in ("typecheck", "lint", "test", "build"):
                    if name in scripts:
                        return f"npm run {name}"
        except Exception:
            pass
    if (repo_root / "pyproject.toml").exists():
        return "pytest"
    if (repo_root / "requirements.txt").exists():
        return "python -m pytest"
    return None


def _summarize_package_json(text: str) -> tuple[str, list[str], list[str]]:
    data = json.loads(text)
    scripts = data.get("scripts", {}) if isinstance(data.get("scripts"), dict) else {}
    deps: list[str] = []
    for key in ("dependencies", "devDependencies"):
        block = data.get(key, {})
        if isinstance(block, dict):
            deps.extend(sorted(block))
    name = data.get("name", "unnamed")
    return (
        f"Node package {name}; scripts: {', '.join(sorted(scripts)[:12]) or 'none'}",
        deps[:16],
        [f"script:{name}" for name in sorted(scripts)[:16]],
    )


def _summarize_pyproject(text: str) -> tuple[str, list[str], list[str]]:
    if tomllib is None:
        return "Python project metadata", [], []
    data = tomllib.loads(text)
    project = data.get("project", {}) if isinstance(data, dict) else {}
    tool = data.get("tool", {}) if isinstance(data, dict) else {}
    name = project.get("name", "unnamed") if isinstance(project, dict) else "unnamed"
    deps = project.get("dependencies", []) if isinstance(project, dict) else []
    tool_names = sorted(tool.keys()) if isinstance(tool, dict) else []
    return (
        f"Python project {name}; tools: {', '.join(tool_names[:12]) or 'none'}",
        [str(dep) for dep in deps[:16]],
        [f"tool:{name}" for name in tool_names[:16]],
    )


def _summarize_python(text: str) -> tuple[str, list[str], list[str]]:
    tree = ast.parse(text)
    symbols: list[str] = []
    contracts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}")
            for dec in node.decorator_list:
                dec_text = ast.unparse(dec) if hasattr(ast, "unparse") else ""
                if any(method in dec_text for method in (".get", ".post", ".put", ".delete", ".patch")):
                    contracts.append(f"route:{node.name}")
    return f"Python source with {len(symbols)} extracted symbols", symbols, contracts


def _summarize_javascript_like(text: str) -> tuple[str, list[str], list[str]]:
    symbols = re.findall(
        r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|interface|type)\s+([A-Za-z_$][\w$]*)",
        text,
    )
    contracts = [f"route:{m}" for m in ("GET", "POST", "PUT", "PATCH", "DELETE") if re.search(rf"\bexport\s+(?:async\s+)?function\s+{m}\b", text)]
    contracts.extend(f"schema:{name}" for name in re.findall(r"\b([A-Za-z_$][\w$]*(?:Schema|Contract))\b", text))
    return f"JavaScript/TypeScript source with {len(symbols)} exported symbols", symbols, contracts


def _cache_path(repo_root: Path, task_type: str) -> Path:
    key = _sha_text(f"{repo_root}::{task_type}")[:24]
    return forge_config.home() / "context-packs" / f"{key}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _git(repo_root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _read_text(path: Path) -> str:
    return path.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", errors="replace")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()[:MAX_FILE_BYTES]).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _kind(path: Path) -> str:
    if path.name == "package.json":
        return "node-manifest"
    if path.name == "pyproject.toml":
        return "python-manifest"
    if path.suffix.lower() in {".md", ".mdc"}:
        return "markdown"
    if path.suffix.lower() == ".py":
        return "python"
    if path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
        return "typescript"
    return "text"
