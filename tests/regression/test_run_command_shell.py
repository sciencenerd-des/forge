"""Regression: shell-shaped run_command must be detected and routed through bash.

The registry ``run_command`` is argv-only; small models routinely emit a shell
line (``>``, ``;``, ``&&``, ``$()``) either as a ``command`` string or packed into
an argv list. Undetected, cmake received ``>/dev/null`` as an argument and failed.
"""
import pytest

pytest.importorskip("langgraph")
from src.nodes.executor_node import (
    _alignment_blocks_tool,
    _normalize_tool_arguments,
    _run_command_string,
    _shellish_run_command,
)


def test_command_string_is_shellish():
    assert _shellish_run_command({"command": "cmake --build build >/dev/null 2>&1"}) is True


def test_argv_list_with_metachars_is_shellish():
    assert _shellish_run_command({"argv": ["cmake", "--build", "build", ">/dev/null"]}) is True


def test_command_as_list_with_metachars_is_shellish():
    # the model sometimes packs a whole shell line into a `command` LIST
    assert _shellish_run_command({"command": ["cmake", "build", "&&", "./x"]}) is True


def test_plain_argv_is_not_shellish():
    assert _shellish_run_command({"argv": ["cmake", "--build", "build"]}) is False


def test_python_dash_c_with_semicolon_stays_argv():
    # The recorded July-13 sandbox failure: a `;` inside a single -c argument is
    # DATA, not a shell operator, so the payload must stay argv (not be routed
    # through a shell that would split the code string).
    payload = {"argv": ["python3", "-c", "import sklearn; print(sklearn.__version__)"]}
    assert _shellish_run_command(payload) is False


def test_python_dash_c_with_parens_and_semicolon_is_not_shellish():
    payload = {"command": ["python3", "-c", "for i in range(3): print(i);"]}
    assert _shellish_run_command(payload) is False


def test_shell_packed_list_quotes_non_operator_tokens():
    # A genuinely shell-packed list (standalone &&) is routed to a shell, and the
    # code argument is quoted so its inner `;` cannot leak into the shell.
    payload = {"argv": ["python3", "-c", "print(1); print(2)", "&&", "echo", "done"]}
    assert _shellish_run_command(payload) is True
    rendered = _run_command_string(payload)
    assert "'print(1); print(2)'" in rendered
    assert " && echo done" in rendered


def test_run_command_string_drops_stray_timeout_token():
    s = _run_command_string({"command": ["echo", "hi", ">x", "timeout_seconds"]})
    assert "timeout_seconds" not in s and "echo hi >x" == s


def test_alignment_classifier_cannot_block_sandboxed_inspection_or_verification():
    for tool in ("list_files", "read_file", "run_command", "bash"):
        assert _alignment_blocks_tool(tool, "distraction") is False
    assert _alignment_blocks_tool("write_file", "distraction") is True


def test_run_command_argv_alias_is_normalized_at_the_executor_boundary():
    assert _normalize_tool_arguments("run_command", {"argv": ["ls", "-R"]}) == {
        "argv": ["ls", "-R"],
        "command": ["ls", "-R"],
    }
