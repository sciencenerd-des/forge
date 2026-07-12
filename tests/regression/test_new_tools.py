"""Pins the run_tests/build/lint/audit_deps tools (engine/src/nodes/executor_node.py).

These close a gap observed repeatedly across live goal reruns: the executor
burning turns retrying blocked bash calls just to run its own test suite, and
struggling to hand-assemble the right CMake/cargo/npm incantation. Each tool
gives the harness a deterministic, stack-aware OSS command instead.

Also pins two real exit-code-masking bugs caught by smoke-testing the actual
container before this ever reached a goal run:
  1. `pytest ... || unittest discover ...` also fires the `unittest` fallback
     on a genuine pytest FAILURE (nonzero exit), and unittest's "Ran 0 tests"
     against pytest-style bare functions reports a false "OK" — silently
     masking the real failure.
  2. `clang-format ... || echo 'not available'` — same shape: a real lint
     violation (nonzero exit) also triggers the `echo` fallback, whose
     always-0 exit becomes the whole command's exit code.
Both are now `if/then/else` so "tool missing" and "tool ran and failed" are
on non-overlapping exit paths — these tests pin that structurally (offline)
and behaviorally (when Docker is available).
"""
import os
import shutil

import pytest

pytest.importorskip("langgraph")
from src.nodes.executor_node import (
    _AUDIT_DEPS_CMD,
    _BUILD_CMD,
    _RUN_TESTS_CMD,
    _detect_project_stack,
    _lint_cmd,
)


class _FakeWorkspace:
    def __init__(self, files):
        self._files = set(files)

    def exists(self, path):
        return path in self._files


def test_detect_stack_prefers_rust_when_cargo_toml_present():
    assert _detect_project_stack(_FakeWorkspace({"Cargo.toml"})) == "rust"


def test_detect_stack_prefers_cpp_when_cmakelists_present():
    assert _detect_project_stack(_FakeWorkspace({"CMakeLists.txt"})) == "cpp"


def test_detect_stack_prefers_node_when_package_json_present():
    assert _detect_project_stack(_FakeWorkspace({"package.json"})) == "node"


def test_detect_stack_defaults_to_python():
    assert _detect_project_stack(_FakeWorkspace(set())) == "python"


def test_detect_stack_precedence_rust_over_others():
    # A project can have multiple marker files (e.g. a Rust project with a
    # package.json for tooling) — Cargo.toml wins.
    assert _detect_project_stack(_FakeWorkspace({"Cargo.toml", "package.json"})) == "rust"


@pytest.mark.parametrize("stack", ["rust", "cpp", "node", "python"])
def test_run_tests_and_build_commands_exist_for_every_templated_stack(stack):
    assert stack in _RUN_TESTS_CMD
    assert stack in _BUILD_CMD
    assert stack in _AUDIT_DEPS_CMD
    assert _lint_cmd(stack, fix=False)


def test_python_run_tests_uses_explicit_if_then_else_not_masking_or():
    # The regression this pins: `pytest ... || unittest ...` (a bare `||`)
    # would ALSO run the unittest fallback on a genuine pytest failure. An
    # explicit if/then/else keeps "pytest missing" and "pytest ran and
    # failed" on separate paths.
    cmd = _RUN_TESTS_CMD["python"]
    assert "if " in cmd and "; then " in cmd and "else " in cmd
    assert '"$VENV" -m pytest -q 2>&1 || "$VENV" -m unittest' not in cmd


def test_cpp_lint_uses_explicit_if_then_else_not_masking_or():
    cmd = _lint_cmd("cpp", fix=False)
    assert "if command -v clang-format" in cmd
    assert "; else " in cmd
    # The old bug: `clang-format ... || echo 'not available'` — a real lint
    # failure (nonzero exit) would also trigger the echo, which always exits
    # 0, masking the failure. The else branch must only ever REPORT
    # unavailability (echo + explicit `false`), never invoke the linter.
    else_branch = cmd.split("; else ")[-1]
    assert "xargs" not in else_branch and "clang-format {mode}" not in else_branch
    assert "false" in else_branch, "the unavailable-tool branch must itself exit nonzero"


def test_node_lint_does_not_invent_a_masking_fallback():
    cmd = _lint_cmd("node", fix=False)
    assert "|| echo" not in cmd  # exit code/output pass through untouched


def test_python_lint_install_guard_does_not_mask_ruff_exit_code():
    # Safe pattern: `||` guards only the INSTALL step; `&&` carries ruff's
    # own exit code through untouched.
    cmd = _lint_cmd("python", fix=False)
    assert "|| pip install" in cmd
    assert cmd.strip().endswith("2>&1")
    assert "ruff check ." in cmd.split("&&")[-1]


def test_lint_fix_flag_threads_through_every_stack():
    for stack in ("rust", "cpp", "node", "python"):
        assert "--fix" in _lint_cmd(stack, fix=True) or "-i" in _lint_cmd(stack, fix=True)


_DOCKER_GATE = pytest.mark.skipif(
    os.environ.get("FORGE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set FORGE_TEST_DOCKER=1 with Docker available to run real tool-command tests",
)


@_DOCKER_GATE
def test_integration_run_tests_reports_real_failure_not_masked():
    from src.nodes.executor_node import _exec_in_sandbox

    from forge_runtime.sandbox import get_workspace, reset_workspace_cache

    reset_workspace_cache()
    ws = get_workspace("test-tools-masking-1", "/tmp/unused")
    try:
        ws.write_text("app.py", "def add(a, b):\n    return a + b\n")
        ws.write_text("test_app.py",
                       "from app import add\ndef test_bug():\n    assert add(2, 2) == 5\n")
        result = _exec_in_sandbox(ws, _RUN_TESTS_CMD["python"], "/tmp/unused", timeout=60)
        assert result.returncode != 0, "a genuinely failing test must not be masked as success"
        assert "assert 4 == 5" in result.stdout or "FAILED" in result.stdout
    finally:
        ws.remove()
        reset_workspace_cache()


@_DOCKER_GATE
def test_integration_run_tests_reports_real_success():
    from src.nodes.executor_node import _exec_in_sandbox

    from forge_runtime.sandbox import get_workspace, reset_workspace_cache

    reset_workspace_cache()
    ws = get_workspace("test-tools-masking-2", "/tmp/unused")
    try:
        ws.write_text("app.py", "def add(a, b):\n    return a + b\n")
        ws.write_text("test_app.py", "from app import add\ndef test_ok():\n    assert add(1, 2) == 3\n")
        result = _exec_in_sandbox(ws, _RUN_TESTS_CMD["python"], "/tmp/unused", timeout=60)
        assert result.returncode == 0
    finally:
        ws.remove()
        reset_workspace_cache()


@_DOCKER_GATE
def test_integration_cpp_lint_reports_real_violation_not_masked():
    from src.nodes.executor_node import _exec_in_sandbox

    from forge_runtime.sandbox import get_workspace, reset_workspace_cache

    reset_workspace_cache()
    ws = get_workspace("test-tools-masking-3", "/tmp/unused")
    try:
        ws.write_text("main.cpp", "int main(  ) {\nint x=1;\nreturn   x;\n}\n")
        result = _exec_in_sandbox(ws, _lint_cmd("cpp", fix=False), "/tmp/unused", timeout=30)
        assert result.returncode != 0, "a genuine formatting violation must not be masked as success"
    finally:
        ws.remove()
        reset_workspace_cache()


@_DOCKER_GATE
def test_integration_build_and_run_tests_full_python_flow():
    from src.nodes.executor_node import _exec_in_sandbox

    from forge_runtime.sandbox import get_workspace, reset_workspace_cache

    reset_workspace_cache()
    ws = get_workspace("test-tools-full-flow", "/tmp/unused")
    try:
        ws.write_text("app.py", "def mul(a, b):\n    return a * b\n")
        ws.write_text("test_app.py", "from app import mul\ndef test_mul():\n    assert mul(2, 3) == 6\n")
        build_result = _exec_in_sandbox(ws, _BUILD_CMD["python"], "/tmp/unused", timeout=60)
        assert build_result.returncode == 0
        test_result = _exec_in_sandbox(ws, _RUN_TESTS_CMD["python"], "/tmp/unused", timeout=60)
        assert test_result.returncode == 0
    finally:
        ws.remove()
        reset_workspace_cache()
