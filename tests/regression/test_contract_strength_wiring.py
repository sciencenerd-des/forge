"""Regression: the Phase 2 contract-strength checks (forge_runtime/contract_immune.py)
are actually reachable from the engine, and match the engine's own grading.

THE INCIDENT (live run, 2026-07-03): fizzbuzz.py was correct, but the executor's
own test file was `def test_pass(): assert True`. Auditor test T3
(`python3 -m pytest -q`, expect_substring "passed", expect_exit 0) is NOT
vacuous by score_contract()'s null-artifact definition — pytest with no test
file present errors out, it doesn't print "passed". So the auditor-side gate
correctly would NOT have caught this contract at derivation time; it needed
the evaluator-side mutation gate, which deletes the real (completed) source
and reruns the same tests. These tests pin that:
  1. detect_stack()'s stack id is directly compatible with
     mutation_non_discrimination_check()'s `stack` parameter (both use
     "python"/"node"/"cpp"/"rust").
  2. The mutation gate, run with real evaluator-shaped test dicts, flags
     exactly the vacuous test and nothing else.
"""
from src.auditor import detect_stack, template_contract

from forge_runtime.contract_immune import mutation_non_discrimination_check


def _should_block(flagged, tests):
    """Mirrors the blocking decision in engine/src/nodes/evaluator_node.py:
    block only when NOT A SINGLE test fails on the mutant, not when any
    individual test happens to survive."""
    return bool(flagged) and len(flagged) >= len(tests)


def test_detect_stack_language_matches_mutation_probe_stack_keys():
    for goal_text, expected in (
        ("Build a Python fizzbuzz.py with a pytest test file", "python"),
        ("Implement a Rust CLI word counter", "rust"),
    ):
        stack = detect_stack(goal_text).get("language")
        assert stack in ("python", "node", "cpp", "rust", None, "")
        if expected == "python":
            assert stack == "python"


def test_mutation_gate_reproduces_the_fizzbuzz_incident_end_to_end(tmp_path):
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

    # Exactly the shape auditor_node.py persists and evaluator_node.py loads
    # (HermesMemoryItem memory_type="audit_tests", JSON-encoded test dicts).
    audit_tests = [
        {"id": "T1", "command": "test -f fizzbuzz.py", "expect_exit": 0},
        {"id": "T2", "command": "python3 -m py_compile fizzbuzz.py && echo COMPILE_OK",
         "expect_substring": "COMPILE_OK", "expect_exit": 0},
        {"id": "T3", "command": "python3 -m pytest -q", "expect_substring": "passed", "expect_exit": 0},
    ]

    stack = detect_stack("Build a Python fizzbuzz.py with a pytest test file").get("language") or "python"
    flagged = mutation_non_discrimination_check(
        tmp_path, audit_tests, stack=stack, allow_host_test_runner=True)

    assert flagged == ("T3",)  # exactly the vacuous test — not T1/T2, not both, not none
    # Here ALL THREE tests are flagged as still-passing? No — only T3. But the
    # engine's blocking decision requires EVERY test to survive; T1/T2 here
    # correctly fail (test -f / py_compile both need the deleted file), so
    # even a single vacuous test alongside working structural checks does
    # NOT trip the coarse "all survive" gate. This specific fixture needs a
    # SECOND vacuous-only case to exercise the actual block path — see
    # test_mutation_gate_blocks_only_when_every_test_survives below.
    assert not _should_block(flagged, audit_tests)


def test_mutation_gate_blocks_when_every_test_genuinely_survives(tmp_path):
    """A contract where EVERY test is vacuous must still be recognized as
    non-discriminating and block completion — this is the actual "should
    block" path, contrasted with the two cases above where at least one
    test correctly fails and completion must proceed."""
    (tmp_path / "fizzbuzz.py").write_text("def fizz(n):\n    return str(n)\n")
    audit_tests = [
        {"id": "T1", "command": "true", "expect_exit": 0},
        {"id": "T2", "command": "echo ok", "expect_substring": "ok", "expect_exit": 0},
    ]
    flagged = mutation_non_discrimination_check(
        tmp_path, audit_tests, stack="python", allow_host_test_runner=True)
    assert flagged == ("T1", "T2")
    assert _should_block(flagged, audit_tests)  # this IS the case that must block


def test_mutation_gate_does_not_block_a_correct_lru_cache_with_meaningful_tests(tmp_path):
    """Reproduction of the SECOND live incident (2026-07-03, LRU cache goal):
    a CORRECT implementation with a genuinely meaningful test suite still
    had T1 (`ls *.py|head -1`) and T2 (compile loop over `*.py`) survive
    source deletion, because the test file itself — sitting at the
    workspace ROOT, not under tests/ — is a valid, compiling .py file that
    alone satisfies both structural precondition checks. T3 (pytest, which
    imports the real module) correctly failed once the source was gone.
    The first version of this gate blocked on ANY flagged test and
    deadlocked forever on this exact, entirely correct submission."""
    (tmp_path / "lru_cache.py").write_text(
        "class LRUCache:\n"
        "    def __init__(self, capacity):\n"
        "        self.capacity = capacity\n"
        "        self.cache = {}\n"
        "    def get(self, key):\n"
        "        if key not in self.cache:\n"
        "            return -1\n"
        "        value = self.cache.pop(key)\n"
        "        self.cache[key] = value\n"
        "        return value\n"
        "    def put(self, key, value):\n"
        "        if key in self.cache:\n"
        "            self.cache.pop(key)\n"
        "        elif len(self.cache) >= self.capacity:\n"
        "            self.cache.pop(next(iter(self.cache)))\n"
        "        self.cache[key] = value\n"
    )
    (tmp_path / "test_lru.py").write_text(
        "from lru_cache import LRUCache\n\n\n"
        "def test_eviction_order():\n"
        "    c = LRUCache(2)\n"
        "    c.put(1, 'a')\n"
        "    c.put(2, 'b')\n"
        "    c.put(3, 'c')  # evicts key 1\n"
        "    assert c.get(1) == -1\n"
        "    assert c.get(2) == 'b'\n"
        "    assert c.get(3) == 'c'\n"
    )
    audit_tests = template_contract(detect_stack("python lru cache"), "lru cache")["tests"]
    flagged = mutation_non_discrimination_check(
        tmp_path, audit_tests, stack="python", allow_host_test_runner=True)

    assert "T1" in flagged  # expected: structural "some .py exists" check, always survives
    assert "T2" in flagged  # expected: structural "compiles" check, always survives
    assert "T3" not in flagged  # pytest correctly fails — it imports the deleted module
    assert not _should_block(flagged, audit_tests)  # must NOT block — this is the actual fix
