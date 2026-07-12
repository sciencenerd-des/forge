"""Regression: the cpp template's base (non-render) contract must require a
real CTest-registered test, not just that the project builds.

Live incident (2026-07-03/04, C++ postfix evaluator goal): `main.cpp` was
`int main() { return 0; }` — no stack, no parsing, no arithmetic — and the
old T1-T4 contract (build-only) marked the goal verified complete. The fed
binary did nothing when given real input.

Verified empirically before wiring this in (docs/LESSONS.md-style caution:
getting a deterministic template wrong deadlocks every future goal on that
stack): `ctest`'s own exit code does NOT discriminate zero-tests-registered
from real tests, in either of two zero-test shapes (no CTestTestfile.cmake
at all, or `enable_testing()` with zero `add_test()` calls) — ctest prints
"No tests were found!!!" and still exits 0. What DOES discriminate: ctest
only prints its "N% tests passed" success summary when at least one test
actually ran; a genuinely failing test exits nonzero. These tests build
real CMake+CTest fixtures (not mocked) and run the actual generated T5
command against each of the three real states.
"""
import shutil
import subprocess

import pytest
from src.auditor import detect_stack, template_contract

CTEST_AVAILABLE = shutil.which("cmake") is not None and shutil.which("ctest") is not None
STACK = detect_stack("c++ cmake postfix evaluator")
T5 = next(t for t in template_contract(STACK, "postfix evaluator")["tests"] if t["id"] == "T5")


def _run_t5(cwd):
    r = subprocess.run(["/bin/bash", "-lc", T5["command"]], cwd=cwd, capture_output=True, text=True, timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    exit_ok = r.returncode == T5["expect_exit"]
    sub_ok = T5["expect_substring"] in out
    return exit_ok and sub_ok, r.returncode, out


def _cmake_project(tmp_path, cmakelists: str, main_cpp: str) -> None:
    (tmp_path / "CMakeLists.txt").write_text(cmakelists)
    (tmp_path / "main.cpp").write_text(main_cpp)
    subprocess.run(["cmake", "-S", str(tmp_path), "-B", str(tmp_path / "build")],
                    capture_output=True, timeout=60)


@pytest.mark.skipif(not CTEST_AVAILABLE, reason="cmake/ctest not available in this environment")
def test_t5_command_generated_by_template():
    assert "ctest" in T5["command"]
    assert T5["expect_substring"] == "tests passed"
    assert T5["expect_exit"] == 0


@pytest.mark.skipif(not CTEST_AVAILABLE, reason="cmake/ctest not available in this environment")
def test_t5_fails_when_no_test_configuration_exists_at_all(tmp_path):
    _cmake_project(
        tmp_path,
        "cmake_minimum_required(VERSION 3.10)\nproject(probe)\nadd_executable(probe main.cpp)\n",
        "int main() { return 0; }",
    )
    passed, exit_code, out = _run_t5(tmp_path)
    assert not passed  # was the exact live incident: builds clean, verifies nothing


@pytest.mark.skipif(not CTEST_AVAILABLE, reason="cmake/ctest not available in this environment")
def test_t5_fails_when_enable_testing_but_zero_add_test(tmp_path):
    _cmake_project(
        tmp_path,
        "cmake_minimum_required(VERSION 3.10)\nproject(probe)\nenable_testing()\nadd_executable(probe main.cpp)\n",
        "int main() { return 0; }",
    )
    passed, exit_code, out = _run_t5(tmp_path)
    assert not passed
    assert exit_code == 0  # the trap: ctest exits 0 even with zero tests registered
    assert "tests passed" not in out


@pytest.mark.skipif(not CTEST_AVAILABLE, reason="cmake/ctest not available in this environment")
def test_t5_passes_for_a_genuinely_registered_and_passing_test(tmp_path):
    _cmake_project(
        tmp_path,
        "cmake_minimum_required(VERSION 3.10)\nproject(probe)\nenable_testing()\n"
        "add_executable(probe main.cpp)\nadd_test(NAME probe_runs COMMAND probe)\n",
        "int main() { return 0; }",
    )
    passed, exit_code, out = _run_t5(tmp_path)
    assert passed
    assert "tests passed" in out


@pytest.mark.skipif(not CTEST_AVAILABLE, reason="cmake/ctest not available in this environment")
def test_t5_fails_for_a_genuinely_registered_but_failing_test(tmp_path):
    _cmake_project(
        tmp_path,
        "cmake_minimum_required(VERSION 3.10)\nproject(probe)\nenable_testing()\n"
        "add_executable(probe main.cpp)\nadd_test(NAME probe_runs COMMAND probe)\n",
        "int main() { return 1; }",  # nonzero -> ctest marks it Failed
    )
    passed, exit_code, out = _run_t5(tmp_path)
    assert not passed
    assert exit_code != 0
