from __future__ import annotations

import json
from pathlib import Path


def _write_node_project(root: Path) -> None:
    (root / "README.md").write_text("# Demo\n\nA small app.\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-app",
                "scripts": {"typecheck": "tsc --noEmit", "test": "vitest"},
                "dependencies": {"react": "^19.0.0"},
            }
        ),
        encoding="utf-8",
    )
    src = root / "src"
    src.mkdir()
    (src / "App.tsx").write_text(
        "export interface UserContract { id: string }\n"
        "export const UserSchema = z.object({ id: z.string() })\n",
        encoding="utf-8",
    )


def test_repo_context_pack_reuses_cache_and_invalidates(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_node_project(repo)

    from forge_runtime.context_pack import build_repo_context_pack

    first = build_repo_context_pack(repo, task_text="build feature")
    second = build_repo_context_pack(repo, task_text="build feature")

    assert first["cache_status"] == "rebuilt"
    assert second["cache_status"] == "hit"
    assert second["repo_fingerprint"] == first["repo_fingerprint"]
    assert second["task_type"] == "feature"
    assert second["last_validation_command"] == "npm run typecheck"
    assert second["steering"]["completion_gate"].startswith("Do not mark the task complete")
    assert second["steering"]["next_actions"]
    assert any(item["path"] == "package.json" for item in second["selected_files"])

    (repo / "README.md").write_text("# Demo\n\nChanged.\n", encoding="utf-8")
    third = build_repo_context_pack(repo, task_text="build feature")

    assert third["cache_status"] == "rebuilt"
    assert third["repo_fingerprint"] != first["repo_fingerprint"]


def test_repo_context_pack_selects_task_relevant_source_files(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / ".forge"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_node_project(repo)
    feature_dir = repo / "src" / "features" / "billing"
    feature_dir.mkdir(parents=True)
    (feature_dir / "invoice-service.ts").write_text(
        "export function buildInvoiceContext() { return 'invoice billing context' }\n",
        encoding="utf-8",
    )

    from forge_runtime.context_pack import build_repo_context_pack

    pack = build_repo_context_pack(
        repo,
        task_text="improve invoice billing context selection",
        max_files=8,
    )

    paths = [item["path"] for item in pack["selected_files"]]
    assert "src/features/billing/invoice-service.ts" in paths
    assert "invoice" in pack["task_focus_terms"]
    assert pack["steering"]["mode"] == "feature"


def test_context_pack_is_threaded_into_memory_service_and_executor_prompt():
    service_source = Path("app/services/__init__.py").read_text(encoding="utf-8")
    executor_source = Path("engine/src/nodes/executor_node.py").read_text(encoding="utf-8")

    assert "from forge_runtime.context_pack import build_repo_context_pack" in service_source
    assert 'pack["REPO_CONTEXT"] = build_repo_context_pack(' in service_source
    assert "REPO CONTEXT PACK" in executor_source
    assert 'repo_context = context_pack.get("REPO_CONTEXT", {})' in executor_source
    assert "json.dumps(repo_context" in executor_source
