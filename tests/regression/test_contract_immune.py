"""Pins Phase 2 of specs/convergent-autonomous-harness.html: contract immune system.

Includes the Lesson-1 regression scenarios (compound shell tests, legitimately
early-passing tests) against the new strength/drift/mutation layer.
"""
import subprocess
import tempfile
from pathlib import Path

import pytest

from forge_runtime.contract_immune import (
    ContractTest,
    collect_review_content,
    contract_hash,
    delete_source_files,
    detect_drift,
    find_source_files,
    find_vacuous_js_test_file,
    find_vacuous_test_functions,
    is_non_discriminating,
    load_semantic_flags,
    mutate_and_check,
    mutation_non_discrimination_check,
    record_semantic_flag,
    review_content_hash,
    review_for_goal_scope_completeness,
    review_for_hardcoded_implementation,
    scan_workspace_for_vacuous_tests,
    score_contract,
)


def _score_in_test_scratch(tests):
    """Test-only runner for literal fixture commands; production uses Docker."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)

        def run_test(test):
            result = subprocess.run(
                ["/bin/bash", "-lc", test.command], cwd=scratch, capture_output=True, text=True,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return (result.returncode == test.expect_exit
                    and (test.expect_substring in output if test.expect_substring else True))

        return score_contract(tests, run_test)


def test_vacuous_test_detected_on_null_workspace():
    tests = [ContractTest("always-true", "true")]
    strength = _score_in_test_scratch(tests)
    assert strength.vacuous_tests == ("always-true",)
    assert not strength.is_strong


def test_strong_contract_requires_built_artifact():
    tests = [ContractTest("file-exists", "test -f built.txt")]
    strength = _score_in_test_scratch(tests)
    assert strength.non_vacuous_count == 1
    assert strength.is_strong


def test_legitimately_early_passing_test_still_counts_non_vacuous_if_others_do():
    # `test -d .git`-style tests can legitimately pass on a fresh checkout;
    # strength only requires *some* non-vacuous test, not that every test is.
    tests = [
        ContractTest("git-exists", "true"),  # vacuous on purpose here
        ContractTest("built", "test -f out.bin"),  # non-vacuous
    ]
    strength = _score_in_test_scratch(tests)
    assert strength.is_strong
    assert "git-exists" in strength.vacuous_tests
    assert "built" not in strength.vacuous_tests


def test_compound_shell_test_is_not_confused_with_missing_binary():
    # Lesson 1: `if ...; then ...; fi` must not be mistaken for a bare `if` binary lookup.
    tests = [ContractTest("compound", "if [ -z \"\" ]; then exit 1; fi")]
    strength = _score_in_test_scratch(tests)
    # This compound test legitimately exits 1 (fails) on the null workspace,
    # so it is correctly non-vacuous, not silently dropped.
    assert "compound" not in strength.vacuous_tests


def test_score_contract_requires_an_isolated_runner():
    with pytest.raises(TypeError):
        score_contract([ContractTest("must-not-run-on-host", "false")])


def test_contract_hash_is_deterministic_and_order_sensitive():
    a = [ContractTest("t1", "true"), ContractTest("t2", "false")]
    b = [ContractTest("t1", "true"), ContractTest("t2", "false")]
    assert contract_hash(a) == contract_hash(b)


def test_dropped_test_is_flagged_as_drift():
    original = [ContractTest("t1", "true"), ContractTest("t2", "false")]
    current = [ContractTest("t1", "true")]
    drift = detect_drift(original, current)
    assert drift.drifted
    assert drift.dropped_tests == ("t2",)


def test_edited_test_command_is_flagged_as_drift():
    original = [ContractTest("t1", "test -f a.txt")]
    current = [ContractTest("t1", "true")]
    drift = detect_drift(original, current)
    assert drift.drifted
    assert drift.edited_tests == ("t1",)


def test_adding_a_new_test_is_not_drift():
    original = [ContractTest("t1", "true")]
    current = [ContractTest("t1", "true"), ContractTest("t2", "false")]
    drift = detect_drift(original, current)
    assert not drift.drifted


def test_mutation_probe_flags_non_discriminating_contract(tmp_path: Path):
    (tmp_path / "out.bin").write_text("payload")
    tests = [ContractTest("always-true", "true")]

    def truncate(mutant: Path) -> None:
        (mutant / "out.bin").unlink()

    still_passing = mutate_and_check(tmp_path, tests, truncate)
    assert is_non_discriminating(still_passing, tests)


def test_mutation_probe_passes_for_discriminating_contract(tmp_path: Path):
    (tmp_path / "out.bin").write_text("payload")
    tests = [ContractTest("artifact-exists", "test -f out.bin")]

    def truncate(mutant: Path) -> None:
        (mutant / "out.bin").unlink()

    still_passing = mutate_and_check(tmp_path, tests, truncate)
    assert not is_non_discriminating(still_passing, tests)
    assert still_passing == ()


def test_mutation_on_workspace_with_no_artifact_yet(tmp_path: Path):
    tests = [ContractTest("artifact-exists", "test -f out.bin")]

    def noop(mutant: Path) -> None:
        pass

    still_passing = mutate_and_check(tmp_path, tests, noop)
    assert still_passing == ()  # already failing, nothing to discriminate


def test_reproduces_the_fizzbuzz_assert_true_incident(tmp_path: Path):
    """Direct reproduction of the live run (2026-07-03): fizzbuzz.py was
    correct, but the executor wrote `def test_pass(): assert True` for
    tests/test_fizzbuzz.py. The evaluator's contract (T1: .py file exists,
    T2: compiles, T3: pytest exits 0) passed all three and declared the goal
    verified — none of the three actually exercised fizzbuzz's logic."""
    (tmp_path / "fizzbuzz.py").write_text(
        "def fizz(n):\n"
        "    if n % 15 == 0: return 'FizzBuzz'\n"
        "    if n % 3 == 0: return 'Fizz'\n"
        "    if n % 5 == 0: return 'Buzz'\n"
        "    return str(n)\n"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fizzbuzz.py").write_text("def test_pass():\n    assert True\n")

    audit_tests = [
        {"id": "T1", "command": "test -f fizzbuzz.py", "expect_exit": 0},
        {"id": "T2", "command": "python3 -m py_compile fizzbuzz.py && echo COMPILE_OK", "expect_substring": "COMPILE_OK", "expect_exit": 0},
        {"id": "T3", "command": "python3 -m pytest -q", "expect_substring": "passed", "expect_exit": 0},
    ]

    flagged = mutation_non_discrimination_check(
        tmp_path, audit_tests, stack="python", allow_host_test_runner=True)
    assert "T3" in flagged  # the vacuous test is caught by id
    assert "T1" not in flagged  # a real file-existence check breaks when the source is gone
    assert "T2" not in flagged  # nothing left to compile


def test_a_real_assertion_survives_the_mutation_probe(tmp_path: Path):
    """Contrast case: a test that actually imports and exercises the module
    is correctly NOT flagged, because it fails once the module is deleted."""
    (tmp_path / "fizzbuzz.py").write_text(
        "def fizz(n):\n    return 'Fizz' if n % 3 == 0 else str(n)\n"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fizzbuzz.py").write_text(
        "from fizzbuzz import fizz\n\n\ndef test_fizz():\n    assert fizz(3) == 'Fizz'\n"
    )
    audit_tests = [
        {"id": "T3", "command": "python3 -m pytest -q", "expect_substring": "passed", "expect_exit": 0},
    ]
    flagged = mutation_non_discrimination_check(
        tmp_path, audit_tests, stack="python", allow_host_test_runner=True)
    assert flagged == ()


def test_empty_workspace_is_not_judged_by_the_mutation_probe(tmp_path: Path):
    audit_tests = [{"id": "T1", "command": "true"}]
    flagged = mutation_non_discrimination_check(
        tmp_path, audit_tests, stack="python", allow_host_test_runner=True)
    assert flagged == ()  # nothing to delete — score_contract()'s job, not this one's


def test_reproduces_the_rpn_calculator_incident(tmp_path: Path):
    """Live incident (2026-07-03, RPN calculator goal): main() was `pass` —
    no parsing, no arithmetic, none of the actual goal — yet the goal was
    marked verified complete. The mutation gate did NOT catch it: the test
    did `from main import main`, so deleting main.py breaks the import,
    which correctly makes the test fail on the mutant — it LOOKS
    discriminating by that measure alone. The vacuous-assertion detector is
    the complementary check that catches it: both test functions assert only
    compile-time constants, so their outcome could never depend on main.py's
    behavior in the first place."""
    (tmp_path / "test_calculator.py").write_text(
        "import pytest\n"
        "from main import main\n\n"
        "def test_main_execution():\n"
        "    \"\"\"Test that the main function can be called without error.\"\"\"\n"
        "    assert True\n\n"
        "def test_calculator_logic():\n"
        "    \"\"\"Placeholder for actual calculator logic tests.\"\"\"\n"
        "    assert 1 + 1 == 2\n"
    )
    vacuous = find_vacuous_test_functions(tmp_path / "test_calculator.py")
    assert vacuous == ("test_main_execution", "test_calculator_logic")


def test_a_function_with_one_real_assertion_is_not_flagged():
    with_mixed_asserts = (
        "def test_something():\n"
        "    assert True  # sanity check\n"
        "    assert compute(2, 3) == 5\n"
    )
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test_mixed.py"
        p.write_text(with_mixed_asserts)
        assert find_vacuous_test_functions(p) == ()  # one real assertion is enough


def test_pytest_raises_without_a_bare_assert_is_not_flagged():
    # A function that only uses `with pytest.raises(...)` has no `assert`
    # statement at all — this check only reasons about asserts, so it
    # correctly declines to judge rather than guessing.
    content = (
        "import pytest\n\n"
        "def test_raises():\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        1 / 0\n"
    )
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test_raises.py"
        p.write_text(content)
        assert find_vacuous_test_functions(p) == ()


def test_scan_workspace_finds_vacuous_tests_across_files(tmp_path: Path):
    (tmp_path / "test_a.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "real_test.py").write_text("def test_y():\n    assert 1 == 1\n")  # matches *_test.py
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_b.py").write_text("def test_z():\n    assert some_func() == 1\n")
    result = scan_workspace_for_vacuous_tests(tmp_path, stack="python")
    # test_a.py and real_test.py are both genuinely vacuous; tests/test_b.py's
    # real function call is correctly NOT flagged.
    assert result == {"test_a.py": ("test_x",), "real_test.py": ("test_y",)}
    assert "tests/test_b.py" not in result


def test_scan_workspace_returns_empty_for_non_python_stacks(tmp_path: Path):
    (tmp_path / "test_a.py").write_text("def test_x():\n    assert True\n")
    assert scan_workspace_for_vacuous_tests(tmp_path, stack="node") == {}


def test_syntax_error_in_test_file_is_ignored_not_crashed(tmp_path: Path):
    bad = tmp_path / "test_broken.py"
    bad.write_text("def test_x(:\n    this is not python\n")
    assert find_vacuous_test_functions(bad) == ()


# --- Node/JS: bare `test.js` misclassification + zero-registration detector ---
# Live incident (2026-07-03, email validator goal): `test.js` was a bare
# `process.exit(0)` — no email-validation logic was ever written (no
# index.js despite package.json declaring it as "main"), yet `npm test`
# exited 0 and the goal was marked verified complete.


def test_bare_test_js_is_recognized_as_a_test_file_not_source(tmp_path: Path):
    (tmp_path / "test.js").write_text("process.exit(0);")
    (tmp_path / "index.js").write_text("module.exports = { validate: () => true };")
    sources = find_source_files(tmp_path, stack="node")
    assert [p.name for p in sources] == ["index.js"]  # test.js must NOT be treated as source


def test_bare_test_js_survives_deletion_pass_protecting_it(tmp_path: Path):
    (tmp_path / "test.js").write_text("process.exit(0);")
    (tmp_path / "index.js").write_text("module.exports = { validate: () => true };")
    deleted = delete_source_files(tmp_path, stack="node")
    assert deleted == 1
    assert (tmp_path / "test.js").exists()  # protected — never a deletion target
    assert not (tmp_path / "index.js").exists()


def test_process_exit_zero_test_js_has_no_registered_test_cases(tmp_path: Path):
    p = tmp_path / "test.js"
    p.write_text("process.exit(0);")
    assert find_vacuous_js_test_file(p) == ("<no test cases registered>",)


def test_js_test_with_registration_but_no_assertions_is_flagged():
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test.js"
        p.write_text(
            "const test = require('node:test');\n"
            "test('does something', () => {\n"
            "  console.log('ran, but never checked anything');\n"
            "});\n"
        )
        assert find_vacuous_js_test_file(p) == ("<no assertion calls found>",)


def test_real_js_test_with_registration_and_assertion_is_not_flagged():
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test.js"
        p.write_text(
            "const test = require('node:test');\n"
            "const assert = require('node:assert');\n"
            "test('validates a real email', () => {\n"
            "  assert.strictEqual(validate('a@b.com'), true);\n"
            "});\n"
        )
        assert find_vacuous_js_test_file(p) == ()


def test_reproduces_the_email_validator_incident(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "email-validator", "main": "index.js"}')
    (tmp_path / "test.js").write_text("process.exit(0);")
    # index.js was NEVER created despite being declared as "main" — the
    # real-world incident had zero implementation at all.
    result = scan_workspace_for_vacuous_tests(tmp_path, stack="node")
    assert result == {"test.js": ("<no test cases registered>",)}


def test_scan_finds_js_tests_under_a_test_directory(tmp_path: Path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "validate.js").write_text("process.exit(0);")
    result = scan_workspace_for_vacuous_tests(tmp_path, stack="node")
    assert result == {"test/validate.js": ("<no test cases registered>",)}


def test_scan_workspace_for_vacuous_tests_unsupported_stack_returns_empty(tmp_path: Path):
    (tmp_path / "test.js").write_text("process.exit(0);")
    assert scan_workspace_for_vacuous_tests(tmp_path, stack="cpp") == {}


# --- Rust: crate-wide `#[test]` count, mirroring the JS zero-registration check ---
# Live incident (2026-07-04, Rust binary search goal): binary_search()
# unconditionally returned None — never implemented — and the crate had
# ZERO #[test] functions anywhere. `cargo test` reported "0 passed; 0
# failed" and exited 0, and the goal was marked verified complete.


def test_reproduces_the_rust_binary_search_incident(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "bs"\nversion = "0.1.0"\n')
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text(
        "fn binary_search(arr: &[i32], target: i32) -> Option<usize> {\n"
        "    // To be implemented\n"
        "    None\n"
        "}\n\n"
        "fn main() {\n    println!(\"Binary search function defined.\");\n}\n"
    )
    result = scan_workspace_for_vacuous_tests(tmp_path, stack="rust")
    assert result == {"<crate>": ("<no #[test] functions found>",)}


def test_rust_crate_with_test_but_no_assertions_is_flagged(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "bs"\nversion = "0.1.0"\n')
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text(
        "fn main() {}\n\n"
        "#[test]\n"
        "fn test_runs_without_error() {\n"
        "    println!(\"ran, but checked nothing\");\n"
        "}\n"
    )
    result = scan_workspace_for_vacuous_tests(tmp_path, stack="rust")
    assert result == {"<crate>": ("<no assertion macros found>",)}


def test_rust_crate_with_real_test_and_assertion_is_not_flagged(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "bs"\nversion = "0.1.0"\n')
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text(
        "fn binary_search(arr: &[i32], target: i32) -> Option<usize> {\n"
        "    arr.iter().position(|&x| x == target)\n"
        "}\n\n"
        "fn main() {}\n\n"
        "#[test]\n"
        "fn test_finds_element() {\n"
        "    assert_eq!(binary_search(&[1, 3, 5], 3), Some(1));\n"
        "}\n"
    )
    result = scan_workspace_for_vacuous_tests(tmp_path, stack="rust")
    assert result == {}


def test_rust_integration_tests_under_tests_dir_are_counted(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "bs"\nversion = "0.1.0"\n')
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "integration.rs").write_text(
        "#[test]\nfn test_add() {\n    assert_eq!(bs::add(2, 3), 5);\n}\n"
    )
    result = scan_workspace_for_vacuous_tests(tmp_path, stack="rust")
    assert result == {}


def test_rust_crate_with_no_rs_files_is_not_judged(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "bs"\nversion = "0.1.0"\n')
    assert scan_workspace_for_vacuous_tests(tmp_path, stack="rust") == {}


# --- LLM-based overfitting review: catches hardcoded-to-the-tests implementations
# that are mechanically indistinguishable from genuine logic (real function call,
# real non-constant assertion) to every check above. Live incident (2026-07-04,
# JSON parser goal): `if x == "null": return None; raise ValueError(...)` — a
# lookup table for exactly the two literal test inputs, nothing else. Uses an
# injected `llm_review` callable for determinism; production wiring passes a
# real cloud-model call (verified separately against gpt-oss:120b-cloud).


def _stub_hardcoded_review(prompt: str) -> dict:
    return {
        "is_hardcoded": True,
        "reasoning": "Only handles the exact literal test inputs.",
        "suspicious_snippets": ['if json_string == "null":'],
    }


def _stub_clean_review(prompt: str) -> dict:
    return {"is_hardcoded": False, "reasoning": "Genuine general algorithm.", "suspicious_snippets": []}


def _stub_failing_review(prompt: str) -> dict:
    raise TimeoutError("cloud call timed out")


def test_collect_review_content_splits_source_from_tests(tmp_path: Path):
    (tmp_path / "parser.py").write_text("def parse(x):\n    return x\n")
    (tmp_path / "test_parser.py").write_text("def test_parse():\n    assert parse(1) == 1\n")
    source, tests = collect_review_content(tmp_path, stack="python")
    assert "def parse(x):" in source
    assert "def test_parse():" in tests
    assert "def parse(x):" not in tests
    assert "def test_parse():" not in source


def test_collect_review_content_respects_char_budget(tmp_path: Path):
    (tmp_path / "big.py").write_text("x = 1\n" * 10000)
    (tmp_path / "test_big.py").write_text("def test_x():\n    assert True\n")
    source, _ = collect_review_content(tmp_path, stack="python", max_chars=500)
    assert len(source) <= 600  # budget-capped, not unbounded


def test_review_flags_the_reproduced_json_parser_incident(tmp_path: Path):
    (tmp_path / "parser.py").write_text(
        "class JSONParser:\n"
        "    def parse(self, json_string):\n"
        "        if json_string == \"null\":\n"
        "            return None\n"
        "        raise ValueError(\"Unsupported input\")\n"
    )
    (tmp_path / "test_parser.py").write_text(
        "from parser import JSONParser\n\n"
        "def test_parse_null():\n"
        "    assert JSONParser().parse(\"null\") is None\n"
    )
    result = review_for_hardcoded_implementation(
        tmp_path, "JSON parser", "parse arbitrary JSON", _stub_hardcoded_review, stack="python"
    )
    assert result["is_hardcoded"] is True
    assert result["suspicious_snippets"]


def test_review_does_not_flag_when_llm_says_clean(tmp_path: Path):
    (tmp_path / "parser.py").write_text("def parse(x):\n    return x\n")
    (tmp_path / "test_parser.py").write_text("def test_parse():\n    assert parse(1) == 1\n")
    result = review_for_hardcoded_implementation(
        tmp_path, "goal", "desc", _stub_clean_review, stack="python"
    )
    assert result == {}


def test_review_reports_llm_failure_instead_of_masquerading_as_a_pass(tmp_path: Path):
    # Live incident (2026-07-05, Tic-Tac-Toe rerun): the cloud call failed,
    # the review returned {} — byte-identical to "reviewed and passed" — and
    # a suite covering 1 of 8 required win conditions completed as verified,
    # with zero trace in the logs. A failed review must be DISTINGUISHABLE
    # from a clean one: the caller still fails open, but visibly, and the
    # semantic-flag-memory gate can compensate for known-bad content.
    (tmp_path / "parser.py").write_text("def parse(x):\n    return x\n")
    (tmp_path / "test_parser.py").write_text("def test_parse():\n    assert parse(1) == 1\n")
    result = review_for_hardcoded_implementation(
        tmp_path, "goal", "desc", _stub_failing_review, stack="python"
    )
    assert "review_error" in result
    assert not result.get("is_hardcoded")  # an outage is never a verdict


def test_review_skips_when_no_source_or_no_tests_exist(tmp_path: Path):
    result = review_for_hardcoded_implementation(
        tmp_path, "goal", "desc", _stub_hardcoded_review, stack="python"
    )
    assert result == {}  # nothing to review — never guesses


# --- LLM-based scope-completeness review: catches a legitimate, non-hardcoded
# implementation that never attempted a core capability the goal explicitly
# asked for. Live incident (2026-07-04, Dijkstra goal): the loop built and
# verified only a Graph data structure — itself genuine, correct work — and
# stopped there, because the deterministic python template contract (some
# file compiles, some pytest passes) never required Dijkstra's algorithm to
# exist. Distinguished from the overfitting review: this asks "is a whole
# capability missing" not "is the code faked to pass tests".


def _stub_scope_incomplete_review(prompt: str) -> dict:
    return {
        "is_scope_incomplete": True,
        "reasoning": "Dijkstra's algorithm itself was never implemented.",
        "missing_requirements": ["Implementation of Dijkstra's shortest path algorithm"],
    }


def _stub_scope_complete_review(prompt: str) -> dict:
    return {"is_scope_incomplete": False, "reasoning": "Every requested capability is attempted.", "missing_requirements": []}


def _stub_scope_review_failure(prompt: str) -> dict:
    raise TimeoutError("cloud call timed out")


def test_scope_review_flags_the_reproduced_dijkstra_incident(tmp_path: Path):
    (tmp_path / "graph.py").write_text(
        "class Graph:\n"
        "    def __init__(self):\n"
        "        self.adjacency_list = {}\n"
        "    def add_edge(self, a, b, w):\n"
        "        self.adjacency_list.setdefault(a, []).append((b, w))\n"
    )
    (tmp_path / "test_graph.py").write_text(
        "from graph import Graph\n\n"
        "def test_add_edge():\n"
        "    g = Graph()\n"
        "    g.add_edge('A', 'B', 5)\n"
        "    assert g.adjacency_list['A'] == [('B', 5)]\n"
    )
    result = review_for_goal_scope_completeness(
        tmp_path,
        "Implement Dijkstra's shortest path algorithm",
        "returning shortest distance and path between two nodes",
        _stub_scope_incomplete_review,
        stack="python",
    )
    assert result["is_scope_incomplete"] is True
    assert result["missing_requirements"]


def test_scope_review_does_not_flag_when_llm_says_complete(tmp_path: Path):
    (tmp_path / "cache.py").write_text("class LRUCache:\n    pass\n")
    (tmp_path / "test_cache.py").write_text("def test_x():\n    assert True\n")
    result = review_for_goal_scope_completeness(
        tmp_path, "goal", "desc", _stub_scope_complete_review, stack="python"
    )
    assert result == {}


def test_scope_review_reports_llm_failure_instead_of_masquerading_as_a_pass(tmp_path: Path):
    # Same live incident class as the overfitting-review counterpart above
    # (2026-07-05, Tic-Tac-Toe rerun): a failed cloud call must not look like
    # a clean verdict.
    (tmp_path / "cache.py").write_text("class LRUCache:\n    pass\n")
    (tmp_path / "test_cache.py").write_text("def test_x():\n    assert True\n")
    result = review_for_goal_scope_completeness(
        tmp_path, "goal", "desc", _stub_scope_review_failure, stack="python"
    )
    assert "review_error" in result
    assert not result.get("is_scope_incomplete")  # an outage is never a verdict


def test_scope_review_skips_when_no_source_or_no_tests_exist(tmp_path: Path):
    result = review_for_goal_scope_completeness(
        tmp_path, "goal", "desc", _stub_scope_incomplete_review, stack="python"
    )
    assert result == {}  # nothing to review — never guesses


# --- Co-located source+test files: the whole-file test classification must
# not blind the LLM reviews. Live incident (2026-07-05, email validator
# rerun): the entire "implementation" — `validateEmail = (email) => email ===
# 'test@example.com'` — lived inside a bare `test.js`, which _is_test_file()
# rightly classifies as a test file (for the mutation probe's sake), leaving
# the review's source bucket EMPTY and silently short-circuiting both LLM
# gates before they ever saw the hardcode.


def test_collect_review_content_reviews_colocated_test_file_as_source(tmp_path: Path):
    (tmp_path / "test.js").write_text(
        "import { test } from 'node:test';\n"
        "const validateEmail = (email) => email === 'test@example.com';\n"
        "test('valid', () => { assert(validateEmail('test@example.com')); });\n"
    )
    source, tests = collect_review_content(tmp_path, stack="node")
    assert "validateEmail" in source  # the hardcode is visible to the review
    assert "validateEmail" in tests


def test_review_flags_the_reproduced_colocated_email_validator_incident(tmp_path: Path):
    (tmp_path / "test.js").write_text(
        "const validateEmail = (email) => email === 'test@example.com';\n"
        "test('valid', () => { assert(validateEmail('test@example.com')); });\n"
    )
    result = review_for_hardcoded_implementation(
        tmp_path, "Validate emails per RFC rules", "", _stub_hardcoded_review, stack="node"
    )
    assert result.get("is_hardcoded") is True  # no longer silently skipped


def test_collect_review_content_separated_files_are_unaffected_by_fallback(tmp_path: Path):
    (tmp_path / "impl.js").write_text("export const f = (x) => x + 1;\n")
    (tmp_path / "test.js").write_text("test('f', () => assert(f(1) === 2));\n")
    source, tests = collect_review_content(tmp_path, stack="node")
    assert "x + 1" in source and "x + 1" not in tests


# --- Mirror case: no file classified as a test AT ALL. Legitimate for the
# CMake+CTest convention (`add_test(NAME x COMMAND the_same_binary)`) — the
# "test" is just running a program and checking its exit code/stdout, not a
# separate file `_is_test_file()` could recognize. Reproduced live
# (2026-07-06, C++ postfix sandbox rerun): a `main.cpp` that printed
# "Test Passed" and exited 0 — with an unused stub `evaluator.cpp` never
# linked into the build, zero postfix-evaluation logic anywhere — was never
# reviewed by either semantic gate, both silently returning `{}` because
# `collect_review_content` found no test-classified content at all.


def test_collect_review_content_reviews_source_as_test_when_no_test_file_exists(tmp_path: Path):
    (tmp_path / "main.cpp").write_text(
        '#include <iostream>\nint main() { std::cout << "Test Passed" << std::endl; return 0; }\n'
    )
    source, tests = collect_review_content(tmp_path, stack="cpp")
    assert "Test Passed" in source
    assert "Test Passed" in tests  # visible to review despite no dedicated test file


def test_review_flags_the_reproduced_cpp_stub_incident(tmp_path: Path):
    (tmp_path / "evaluator.cpp").write_text(
        '#include <iostream>\nint main() { std::cout << "Hello, Postfix Evaluator" << std::endl; return 0; }\n'
    )
    (tmp_path / "main.cpp").write_text(
        '#include <iostream>\nint main() { std::cout << "Test Passed" << std::endl; return 0; }\n'
    )
    overfit = review_for_hardcoded_implementation(
        tmp_path, "Implement a stack-based postfix expression evaluator in C++",
        "with a test executable verifying arithmetic results", _stub_hardcoded_review, stack="cpp",
    )
    assert overfit.get("is_hardcoded") is True

    scope = review_for_goal_scope_completeness(
        tmp_path, "Implement a stack-based postfix expression evaluator in C++",
        "with a test executable verifying arithmetic results", _stub_scope_incomplete_review, stack="cpp",
    )
    assert scope.get("is_scope_incomplete") is True


# --- Build-artifact exclusion: generated files must never be what the review
# gates are shown. Live incident (2026-07-05, C++ postfix rerun): the `*.cpp`
# rglob returned CMake's autogenerated compiler-probe
# (build/CMakeFiles/**/CMakeCXXCompilerId.cpp) instead of src/evaluator.cpp,
# so both LLM gates kept flagging "this is a compiler identification file" no
# matter what the executor wrote — an unbounded stagnation loop (35+ batches).


def test_review_content_skips_cmake_build_directories(tmp_path: Path):
    probe = tmp_path / "build" / "CMakeFiles" / "4.0.3" / "CompilerIdCXX"
    probe.mkdir(parents=True)
    (probe / "CMakeCXXCompilerId.cpp").write_text("/* compiler probe */ int main() { return 0; }\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "evaluator.cpp").write_text("double evaluatePostfix(const std::string& e) { return 0; }\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.cpp").write_text("int main() { return 0; }\n")
    source, _ = collect_review_content(tmp_path, stack="cpp")
    assert "evaluatePostfix" in source
    assert "compiler probe" not in source


def test_mutation_probe_does_not_delete_files_under_build(tmp_path: Path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.cpp").write_text("int x;\n")
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    files = find_source_files(tmp_path, stack="cpp")
    assert all("build" not in p.parts for p in files)
    assert any(p.name == "main.cpp" for p in files)


# --- Semantic-flag memory: content a semantic review already rejected must
# not complete unchanged. Live incidents (2026-07-05, LRU-cache and
# JSON-parser reruns): scope gate blocks → executor's genuine fix regresses a
# mechanical test → vanishing-test guard auto-reverts to the exact
# just-rejected content → goal completes against it (cloud outage or verdict
# nondeterminism). The memory is keyed by a hash of exactly what the reviews
# are shown and stored inside .git/, which `git checkout -- . && git clean
# -fd` (the revert) never touches.


def test_review_content_hash_is_stable_and_changes_with_content(tmp_path: Path):
    (tmp_path / "cache.py").write_text("class LRUCache:\n    pass\n")
    (tmp_path / "test_cache.py").write_text("def test_x():\n    assert True\n")
    h1 = review_content_hash(tmp_path, stack="python")
    h2 = review_content_hash(tmp_path, stack="python")
    assert h1 and h1 == h2
    (tmp_path / "test_cache.py").write_text("def test_zero_capacity():\n    assert LRUCache(0)\n")
    assert review_content_hash(tmp_path, stack="python") != h1


def test_review_content_hash_empty_when_nothing_reviewable(tmp_path: Path):
    assert review_content_hash(tmp_path, stack="python") == ""


def test_semantic_flags_roundtrip_and_survive_the_revert_commands(tmp_path: Path):
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    record_semantic_flag(tmp_path, "abc123", "scope incomplete: no draw test")
    assert load_semantic_flags(tmp_path) == {"abc123": "scope incomplete: no draw test"}
    # The vanishing-test guard's exact revert sequence must not erase memory.
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "--", "."], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "clean", "-fd"], capture_output=True)
    assert load_semantic_flags(tmp_path) == {"abc123": "scope incomplete: no draw test"}


def test_semantic_flags_noop_without_a_git_directory(tmp_path: Path):
    record_semantic_flag(tmp_path, "abc123", "reason")  # no .git — must not raise
    assert load_semantic_flags(tmp_path) == {}


# --- Reviewer blindness to build manifests. Reproduced live (2026-07-07,
# C++ postfix v3 stress run): T3/T4/T5 all PASSED — cmake configure, build,
# and ctest succeeded, so CMakeLists.txt provably existed — yet the scope
# reviewer repeatedly flagged "no CMakeLists.txt or any CMake configuration",
# because collect_review_content only globbed source extensions and the
# reviewer literally could not see build files. The executor was then told,
# over and over, to add CMake config that already existed — a destructive
# wild-goose chase ending with the real evaluator regressed to a Hello-World
# placeholder. A reviewer shown an incomplete workspace picture doesn't fail
# safe: it hallucinates missing capabilities with full confidence.


def test_review_content_includes_cmake_manifests(tmp_path: Path):
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(x)\nenable_testing()\nadd_test(NAME t COMMAND t)\n")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "eval.cpp").write_text("double eval() { return 1; }\n")
    source, tests = collect_review_content(tmp_path, stack="cpp")
    combined = source + tests
    assert "enable_testing()" in combined, "the reviewer must see the build configuration it judges"


def test_review_content_includes_cargo_and_package_manifests(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    source, tests = collect_review_content(tmp_path, stack="rust")
    assert 'name = "x"' in (source + tests)


def test_review_content_still_excludes_generated_build_dirs(tmp_path: Path):
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    build = tmp_path / "build"
    build.mkdir()
    (build / "Makefile").write_text("# cmake-generated, must not be reviewed\n")
    (build / "CMakeLists.txt").write_text("# copied into build dir by cmake\n")
    source, tests = collect_review_content(tmp_path, stack="cpp")
    assert "cmake-generated" not in (source + tests)
    assert "copied into build dir" not in (source + tests)
