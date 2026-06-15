"""Regression: shell-shaped run_command must be detected and routed through bash.

The registry ``run_command`` is argv-only; small models routinely emit a shell
line (``>``, ``;``, ``&&``, ``$()``) either as a ``command`` string or packed into
an argv list. Undetected, cmake received ``>/dev/null`` as an argument and failed.
"""
import pytest

pytest.importorskip("langgraph")
from src.nodes.executor_node import _shellish_run_command, _run_command_string


def test_command_string_is_shellish():
    assert _shellish_run_command({"command": "cmake --build build >/dev/null 2>&1"}) is True


def test_argv_list_with_metachars_is_shellish():
    assert _shellish_run_command({"argv": ["cmake", "--build", "build", ">/dev/null"]}) is True


def test_command_as_list_with_metachars_is_shellish():
    # the model sometimes packs a whole shell line into a `command` LIST
    assert _shellish_run_command({"command": ["cmake", "build", "&&", "./x"]}) is True


def test_plain_argv_is_not_shellish():
    assert _shellish_run_command({"argv": ["cmake", "--build", "build"]}) is False


def test_run_command_string_drops_stray_timeout_token():
    s = _run_command_string({"command": ["echo", "hi", ">x", "timeout_seconds"]})
    assert "timeout_seconds" not in s and "echo hi >x" == s
