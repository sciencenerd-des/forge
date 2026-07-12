"""Regression: templated and LLM-authored contract tests must not have their
real exit code masked by a later pipe stage (head/tail/etc.).

THE BUG (found live, 2026-07-03): the python template's T1
(`ls *.py | head -1`, expect_exit 0) passed even when there was NO .py file
in the workspace, because `head -1` on empty input still exits 0 — the
failing exit code from `ls` never propagated past the pipe. The rust
template's T1 was worse: `cargo build 2>&1 | tail -3 && echo BUILD_OK`
printed BUILD_OK on every run, including failed builds, because `&&` only
sees `tail`'s (always-0) exit status.
"""
import subprocess

from src.auditor import detect_stack, harden_pipefail, template_contract


def _run(cmd, cwd):
    return subprocess.run(["/bin/bash", "-lc", cmd], cwd=cwd, capture_output=True, text=True)


def test_harden_pipefail_repairs_unsafe_pipe_and_returns_its_id():
    tests = [{"id": "T1", "command": "ls *.py | head -1"}]
    hardened, repaired = harden_pipefail(tests)
    assert repaired == ["T1"]
    assert hardened[0]["command"].startswith("set -o pipefail;")


def test_harden_pipefail_is_a_noop_for_pipe_free_commands():
    tests = [{"id": "T1", "command": "test -f fizzbuzz.py"}]
    hardened, repaired = harden_pipefail(tests)
    assert repaired == []
    assert hardened == tests


def test_harden_pipefail_does_not_double_prefix_already_safe_commands():
    tests = [{"id": "T1", "command": "set -o pipefail; ls *.py | head -1"}]
    hardened, repaired = harden_pipefail(tests)
    assert repaired == []
    assert hardened[0]["command"].count("pipefail") == 1


def test_python_template_t1_now_fails_on_empty_workspace(tmp_path):
    tests = template_contract(detect_stack("python fizzbuzz"), "fizzbuzz")["tests"]
    t1 = next(t for t in tests if t["id"] == "T1")
    r = _run(t1["command"], tmp_path)
    assert r.returncode != 0  # was 0 before the fix — false pass on nothing


def test_python_template_t1_passes_when_a_py_file_exists(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    tests = template_contract(detect_stack("python fizzbuzz"), "fizzbuzz")["tests"]
    t1 = next(t for t in tests if t["id"] == "T1")
    r = _run(t1["command"], tmp_path)
    assert r.returncode == 0
    assert ".py" in (r.stdout or "")


def test_rust_template_t1_no_longer_reports_build_ok_on_a_failed_build(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "broken"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.cpp").write_text("this is not valid rust at all !!!")  # no main.rs -> cargo build fails
    tests = template_contract(detect_stack("rust cargo project"), "rust cli")["tests"]
    t1 = next(t for t in tests if t["id"] == "T1")
    r = _run(t1["command"], tmp_path)
    assert "BUILD_OK" not in (r.stdout or "")  # was unconditionally printed before the fix


def test_cpp_template_t2_fails_when_no_cpp_source_exists(tmp_path):
    # T2 is now `find ... | head -1`: `find` exits 0 even with zero matches
    # (finding nothing is not an error), so the real discriminator is the
    # evaluator's `expect_substring` check on empty stdout, not the raw
    # exit code — grade it exactly as engine/src/nodes/evaluator_node.py
    # does (exit code AND substring), not exit code alone.
    tests = template_contract(detect_stack("c++ cmake project"), "cpp app")["tests"]
    t2 = next(t for t in tests if t["id"] == "T2")
    r = _run(t2["command"], tmp_path)
    exit_ok = r.returncode == t2["expect_exit"]
    sub_ok = t2["expect_substring"] in (r.stdout or "")
    assert not (exit_ok and sub_ok)  # must NOT pass on an empty workspace


def test_cpp_template_t2_does_not_spuriously_fail_when_cpp_only_lives_under_src(tmp_path):
    """Regression for a bug the pipefail fix itself introduced and exposed
    live (2026-07-03, C++ postfix evaluator goal): the OLD T2 command was
    `ls *.cpp src/*.cpp | head -1`. When only `src/*.cpp` matches (a
    completely normal, idiomatic layout — no .cpp at the repo root), `ls`
    given several glob patterns exits nonzero overall because ONE of its
    arguments didn't match, even though it printed the file that did. Before
    the pipefail fix this was silently masked by `| head -1` (always exit
    0); after the fix, `set -o pipefail` correctly propagated that spurious
    nonzero exit — turning a previously-silent bug into an actively
    blocking one on an entirely correct project. `find` has no such
    multi-pattern partial-match ambiguity."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.cpp").write_text("int main() { return 0; }")
    tests = template_contract(detect_stack("c++ cmake project"), "cpp app")["tests"]
    t2 = next(t for t in tests if t["id"] == "T2")
    r = _run(t2["command"], tmp_path)
    assert r.returncode == 0
    assert ".cpp" in (r.stdout or "")


def test_python_template_t3_no_longer_masks_a_real_pytest_failure(tmp_path):
    """Regression (found live, 2026-07-06, LRU-cache sandbox rerun): the OLD
    T3 command was `pytest -q ... | tail -3 || unittest discover ... | tail -3`.
    `set -o pipefail` correctly made the pytest pipeline's own exit code
    nonzero on a real test failure, but the outer `||` STILL fired on that
    nonzero exit, falling through to `unittest discover` — which finds zero
    pytest-style bare-function tests ("Ran 0 tests ... OK", exit 0) and
    becomes the reported result. A goal with a genuinely broken
    implementation (2 of 3 tests failing on a real LRU-eviction bug) was
    marked verified complete over it. The same failure shape as Lesson 13's
    run_tests/lint fix, found independently in the auditor's own template."""
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_app.py").write_text(
        "from app import add\n"
        "def test_ok():\n    assert add(1, 2) == 3\n"
        "def test_broken():\n    assert add(2, 2) == 5\n"
    )
    tests = template_contract(detect_stack("python app"), "app")["tests"]
    t3 = next(t for t in tests if t["id"] == "T3")
    r = _run(t3["command"], tmp_path)
    assert r.returncode != 0, "a genuinely failing pytest suite must not be masked as success"
    assert "test_broken" in (r.stdout or "") or "FAILED" in (r.stdout or "")


def test_python_template_t3_passes_on_a_genuinely_correct_implementation(tmp_path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_app.py").write_text(
        "from app import add\ndef test_ok():\n    assert add(1, 2) == 3\n"
    )
    tests = template_contract(detect_stack("python app"), "app")["tests"]
    t3 = next(t for t in tests if t["id"] == "T3")
    r = _run(t3["command"], tmp_path)
    assert r.returncode == 0


def test_cpp_template_t4_surfaces_compiler_diagnostics_on_failure(tmp_path):
    """Regression (found live, 2026-07-06/07, C++ postfix sandbox rerun): the
    OLD T3/T4 commands were `cmake ... >/dev/null 2>&1 && echo BUILD_OK` —
    they discarded ALL compiler output unconditionally. On failure the
    captured output was the empty string, so every repair task read
    `current output: ''` and the executor burned 32 attempts on a one-line
    missing `#include <string>` whose fix GCC named verbatim in the
    diagnostics the contract itself was throwing away. The loop wasn't
    incapable — it was blind by its own contract's design. On failure the
    command must now surface the compiler's error text (error lines LAST,
    since every downstream consumer truncates keeping the end); on success
    it must still print only the marker."""
    import shutil as _sh
    if _sh.which("cmake") is None:
        import pytest as _pt
        _pt.skip("cmake not available on this host")
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(x)\nadd_executable(x main.cpp)\n")
    # Missing #include <string> — the exact live incident.
    (tmp_path / "main.cpp").write_text(
        "struct E { double eval(const std::string& s); };\nint main() { return 0; }\n")
    tests = template_contract(detect_stack("c++ cmake project"), "cpp app")["tests"]
    t3 = next(t for t in tests if t["id"] == "T3")
    t4 = next(t for t in tests if t["id"] == "T4")
    assert _run(t3["command"], tmp_path).returncode == 0  # configure is fine
    r4 = _run(t4["command"], tmp_path)
    assert r4.returncode != 0
    assert "error" in (r4.stdout or "").lower(), "compiler diagnostics must reach the evaluator"
    assert "string" in (r4.stdout or ""), "the actionable line (missing <string>) must be visible"


def test_cpp_template_t4_prints_only_the_marker_on_success(tmp_path):
    import shutil as _sh
    if _sh.which("cmake") is None:
        import pytest as _pt
        _pt.skip("cmake not available on this host")
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(x)\nadd_executable(x main.cpp)\n")
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    tests = template_contract(detect_stack("c++ cmake project"), "cpp app")["tests"]
    t3 = next(t for t in tests if t["id"] == "T3")
    t4 = next(t for t in tests if t["id"] == "T4")
    assert _run(t3["command"], tmp_path).returncode == 0
    r4 = _run(t4["command"], tmp_path)
    assert r4.returncode == 0
    assert "BUILD_OK" in r4.stdout
