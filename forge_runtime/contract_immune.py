"""Contract immune system: strength scoring, drift detection, mutation probes.

Hardens the reward channel against the failure class described as Lesson 1 in
docs/LESSONS.md — a contract silently weakened until it is trivially
satisfiable. Three defenses:

1. ``score_contract`` — probes each test against a *null* (empty) artifact.
   A test that passes with nothing built is vacuous: it proves nothing.
2. ``contract_hash`` / drift detection — the test list is content-hashed at
   derivation; any later divergence must be an explicit, logged amendment,
   never a silent drop.
3. ``mutate_and_check`` — applies a caller-supplied mutation to a *copy* of
   the workspace and asserts at least one test now fails, i.e. the contract
   actually discriminates between "done" and "not done".
"""
from __future__ import annotations

import ast
import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class ContractTest:
    __test__ = False  # not a pytest test case despite the name prefix

    test_id: str
    command: str
    expect_substring: str = ""
    expect_exit: int = 0


@dataclass(frozen=True)
class ContractStrength:
    vacuous_tests: tuple[str, ...]
    non_vacuous_count: int
    total_count: int

    @property
    def is_strong(self) -> bool:
        """At least one non-vacuous test and not everything is vacuous."""
        return self.total_count > 0 and self.non_vacuous_count > 0


def _run(command: str, cwd: Path, timeout: int = 30) -> bool:
    """Bare exit-code check (used by the mutation probe, where pass == exit 0)."""
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(cwd), timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _run_graded(test: ContractTest, cwd: Path, timeout: int = 60, env: dict | None = None) -> bool:
    """Mirrors engine/src/nodes/evaluator_node.py's exact grading: exit code
    must match ``expect_exit`` AND ``expect_substring`` (if set) must appear
    in combined stdout+stderr. Keeping this identical to the evaluator is
    what makes the strength score trustworthy — a probe with looser grading
    than production would under- or over-report vacuousness."""
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", test.command], cwd=str(cwd), timeout=timeout,
            capture_output=True, text=True, env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        exit_ok = proc.returncode == test.expect_exit
        sub_ok = (test.expect_substring in out) if test.expect_substring else True
        return exit_ok and sub_ok
    except (subprocess.TimeoutExpired, OSError):
        return False


def score_contract(
    tests: Sequence[ContractTest],
    run_in_empty_workspace: Callable[[ContractTest], bool],
) -> ContractStrength:
    """Score a contract with a caller-provided isolated runner.

    Contract commands are model-generated input.  This function deliberately
    does not create a host subprocess: production callers must supply a
    container-backed runner for the empty-workspace probe.
    """
    vacuous = [t.test_id for t in tests if run_in_empty_workspace(t)]
    non_vacuous = len(tests) - len(vacuous)
    return ContractStrength(
        vacuous_tests=tuple(vacuous),
        non_vacuous_count=non_vacuous,
        total_count=len(tests),
    )


def contract_hash(tests: Sequence[ContractTest]) -> str:
    """Deterministic content hash of the ordered test list (id + command)."""
    payload = "\n".join(f"{t.test_id}:{t.command}" for t in tests)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DriftResult:
    drifted: bool
    dropped_tests: tuple[str, ...]
    edited_tests: tuple[str, ...]


def detect_drift(
    original: Sequence[ContractTest], current: Sequence[ContractTest]
) -> DriftResult:
    """Compare two versions of a contract's test list; never silently drop.

    A test disappearing entirely, or reappearing with a different command
    under the same id, is drift. Adding new tests is not drift.
    """
    orig_by_id = {t.test_id: t.command for t in original}
    cur_by_id = {t.test_id: t.command for t in current}

    dropped = tuple(sorted(set(orig_by_id) - set(cur_by_id)))
    edited = tuple(
        sorted(
            tid for tid in (set(orig_by_id) & set(cur_by_id))
            if orig_by_id[tid] != cur_by_id[tid]
        )
    )
    return DriftResult(drifted=bool(dropped or edited), dropped_tests=dropped, edited_tests=edited)


def mutate_and_check(
    workspace: Path,
    tests: Sequence[ContractTest],
    mutate: Callable[[Path], None],
) -> tuple[str, ...]:
    """Apply ``mutate`` to a copy of the workspace; return test_ids that still
    pass on the mutant (a contract that discriminates should have few/none)."""
    with tempfile.TemporaryDirectory(prefix="forge-mutation-") as tmp:
        mutant = Path(tmp) / "mutant"
        shutil.copytree(workspace, mutant)
        mutate(mutant)
        still_passing = tuple(t.test_id for t in tests if _run_graded(t, cwd=mutant))
        return still_passing


def is_non_discriminating(still_passing: Sequence[str], tests: Sequence[ContractTest]) -> bool:
    """True if the mutation didn't break a single test — contract can't tell done from broken."""
    return bool(tests) and len(still_passing) == len(tests)


# ---------------------------------------------------------------------------
# Live-workspace mutation probe (post-completion gate)
#
# score_contract() answers "could this test ever pass with zero effort?" —
# useful at contract-derivation time, before any artifact exists. It CANNOT
# catch a test the executor writes with real content that still verifies
# nothing (e.g. ``def test_pass(): assert True``) — that test legitimately
# fails against an empty workspace, so it isn't vacuous by that definition.
#
# The functions below close that gap: they run against a COPY of the real,
# completed workspace with its non-test source files deleted. A test that
# still passes after the thing it claims to verify no longer exists proves
# nothing about that thing — it is flagged individually, by id, rather than
# judging the whole contract pass/fail.
# ---------------------------------------------------------------------------

_TEST_DIR_MARKERS = {"tests", "test", "__tests__"}
_SOURCE_GLOBS = {
    "python": ("*.py",),
    "node": ("*.js", "*.ts"),
    "cpp": ("*.cpp", "*.cc", "*.h", "*.hpp"),
    "rust": ("*.rs",),
}
# Build/config manifests the LLM reviews must ALSO see. Reproduced live
# (2026-07-07, C++ postfix v3 stress run): T3/T4/T5 all PASSED — cmake
# configure, build, and ctest succeeded, so CMakeLists.txt provably existed —
# yet the scope reviewer kept flagging "the repository contains no
# CMakeLists.txt or any CMake configuration", because collect_review_content
# only globbed *.cpp/*.h and the reviewer literally could not see the build
# files. The executor was then repeatedly told to add CMake config that was
# already there — a destructive wild-goose chase that ended with it
# regressing the real evaluator to a Hello-World placeholder. A reviewer
# shown an incomplete picture of the workspace doesn't fail safe: it
# hallucinates missing capabilities with full confidence.
_MANIFEST_GLOBS = ("CMakeLists.txt", "Cargo.toml", "package.json",
                   "pyproject.toml", "setup.py", "Makefile")
# "build"/"CMakeFiles"/"cmake-build-*" matter beyond hygiene: reproduced live
# (2026-07-05, C++ postfix rerun) — collect_review_content()'s `*.cpp` rglob
# picked up CMake's autogenerated compiler-probe file
# (build/CMakeFiles/.../CMakeCXXCompilerId.cpp) INSTEAD of the real
# src/evaluator.cpp, so the LLM review gates kept (correctly, given what they
# were shown) flagging "this is a compiler identification file, not an
# evaluator" no matter what the executor wrote — an unbounded stagnation loop
# (35+ batches) on a goal whose actual source was fine.
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "target",
              "build", "CMakeFiles", "cmake-build-debug", "cmake-build-release", "dist"}


def _is_test_file(relative_path: Path) -> bool:
    parts = {p.lower() for p in relative_path.parts[:-1]}
    if parts & _TEST_DIR_MARKERS:
        return True
    name = relative_path.name.lower()
    stem = relative_path.stem.lower()
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
        # Bare `test.js` / `test.ts` (no separator) — the live incident
        # (2026-07-03, email validator goal): `test.js` was misclassified as
        # a SOURCE file (matched `*.js` but none of the separator-based
        # patterns above), so the mutation probe deleted it instead of
        # protecting it — the probe then "passed" for the wrong reason
        # (npm test failed only because test.js itself was gone, not
        # because any real validation logic broke).
        or stem == "test"
    )


def find_source_files(workspace: Path, stack: str = "python") -> tuple[Path, ...]:
    """Every non-test source file for ``stack`` under ``workspace``."""
    globs = _SOURCE_GLOBS.get(stack, _SOURCE_GLOBS["python"])
    found = []
    for pattern in globs:
        for p in workspace.rglob(pattern):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            if _is_test_file(p.relative_to(workspace)):
                continue
            found.append(p)
    return tuple(found)


def delete_source_files(workspace: Path, stack: str = "python") -> int:
    """Mutation: delete every non-test source file. Returns the count deleted."""
    files = find_source_files(workspace, stack)
    for f in files:
        f.unlink()
    return len(files)


def run_dict_tests(
    tests: Sequence[dict], workspace: Path, timeout: int = 60, env: dict | None = None
) -> tuple[dict, ...]:
    """Run auditor-shaped test dicts (id/command/expect_substring/expect_exit)
    against ``workspace``, grading identically to evaluator_node.py."""
    results = []
    for t in tests:
        test = ContractTest(
            test_id=str(t.get("id", "?")),
            command=t.get("command", ""),
            expect_substring=t.get("expect_substring") or "",
            expect_exit=int(t.get("expect_exit") or 0),
        )
        results.append({"id": test.test_id, "passed": _run_graded(test, cwd=workspace, timeout=timeout, env=env)})
    return tuple(results)


def mutation_non_discrimination_check(
    workspace: Path,
    tests: Sequence[dict],
    stack: str = "python",
    timeout: int = 60,
    env: dict | None = None,
    *,
    allow_host_test_runner: bool = False,
) -> tuple[str, ...]:
    """Delete every non-test source file in a COPY of ``workspace``; rerun the
    audit tests there. Returns the ids of tests that still pass despite the
    artifact being gone — those tests verify nothing about the deliverable
    and should not be trusted to gate goal completion.

    Returns ``()`` (never flags) if there is nothing to delete — an empty
    workspace can't be judged this way; that's score_contract()'s job.
    """
    if not allow_host_test_runner:
        raise RuntimeError("mutation probe requires a container-backed runner")
    with tempfile.TemporaryDirectory(prefix="forge-mutation-eval-") as tmp:
        mutant = Path(tmp) / "mutant"
        shutil.copytree(workspace, mutant, ignore=shutil.ignore_patterns(*_SKIP_DIRS))
        deleted = delete_source_files(mutant, stack)
        if deleted == 0:
            return ()
        results = run_dict_tests(tests, mutant, timeout=timeout, env=env)
        return tuple(r["id"] for r in results if r["passed"])


# ---------------------------------------------------------------------------
# Vacuous-assertion detector (static, python-only for now)
#
# The mutation gate above proves a NECESSARY condition — "this test's outcome
# depends on the source existing" — by checking it fails once the source is
# deleted. It is not SUFFICIENT: `from main import main; def test(): assert
# True` correctly fails on the mutant (the bare import throws), which makes
# it look discriminating, even though its assertion never calls `main()` or
# checks anything about its behavior — the test could pass against ANY
# implementation, correct or not, as long as the module imports cleanly.
#
# Reproduced live (2026-07-03, RPN calculator goal): main() was `pass` — no
# parsing, no arithmetic, none of the actual goal — and the goal was still
# marked verified complete, because both of its tests were `assert True` and
# `assert 1 + 1 == 2`. Neither test's outcome could ever depend on main.py's
# behavior; only on whether it imports at all.
#
# This is a different, complementary defense: statically prove an assertion
# is VACUOUS — its truth value is fixed at parse time from literals alone,
# so no runtime behavior (real or broken) could ever change its outcome.
# ---------------------------------------------------------------------------


def _is_constant_expr(node: ast.AST) -> bool:
    """True if ``node`` evaluates to a fixed value regardless of any code
    under test — built only from literals and constant-folding-safe
    operators. A Name, Call, Attribute, or Subscript makes it NOT constant,
    since any of those could reference the code being tested."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.BinOp, ast.UnaryOp)):
        return all(
            _is_constant_expr(child)
            for child in ast.iter_child_nodes(node)
            if isinstance(child, ast.expr)
        )
    if isinstance(node, ast.BoolOp):
        return all(_is_constant_expr(v) for v in node.values)
    if isinstance(node, ast.Compare):
        return _is_constant_expr(node.left) and all(_is_constant_expr(c) for c in node.comparators)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_constant_expr(el) for el in node.elts)
    return False


def find_vacuous_test_functions(test_file: Path) -> tuple[str, ...]:
    """Parse a python test file; return the names of ``test_*`` functions
    whose EVERY assert statement is a compile-time constant.

    A function with even one non-constant assertion is NOT flagged — this
    only fires when it can PROVE every assertion is vacuous, never on a
    guess. A function with no assert statements at all (e.g. it only uses
    ``pytest.raises``) is also not flagged, since this check only reasons
    about ``assert`` statements.
    """
    try:
        tree = ast.parse(test_file.read_text())
    except (SyntaxError, OSError, UnicodeDecodeError):
        return ()
    vacuous = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        if asserts and all(_is_constant_expr(a.test) for a in asserts):
            vacuous.append(node.name)
    return tuple(vacuous)


_JS_TEST_REGISTRATION_RE = re.compile(r"\b(?:test|it|describe)\s*\(\s*['\"`]")
_JS_ASSERTION_RE = re.compile(r"\b(?:assert(?:\.\w+)?|expect)\s*\(")
_JS_TEST_GLOBS = ("test.js", "test.ts", "*.test.js", "*.test.ts", "*.spec.js", "*.spec.ts")

_RUST_TEST_ATTR_RE = re.compile(r"#\[test\]")
_RUST_ASSERTION_RE = re.compile(r"\bassert(?:_eq|_ne)?!\s*\(")


def scan_rust_crate_for_vacuous_tests(workspace: Path) -> dict:
    """Rust has no fixed "test file" naming convention — `#[test]` functions
    can live in ANY `.rs` file (inline unit tests in `src/*.rs`, or
    integration tests under `tests/*.rs`). `cargo test` happily reports
    `running 0 tests` / `0 passed; 0 failed` and exits 0 whether or not any
    test ever existed — an empty crate satisfies it exactly as well as a
    fully-tested one.

    Live incident this pins (2026-07-04, Rust binary search goal):
    `binary_search()` unconditionally returned `None` — never implemented —
    and the crate had ZERO `#[test]` functions anywhere. `cargo test`
    reported `0 passed; 0 failed` and the goal was marked verified complete.

    Judged crate-wide (like the JS check, coarser than the python AST walk,
    since there's no bundled Rust parser): if the crate has zero `#[test]`
    attributes at all, OR has some but zero assertion macro calls anywhere,
    the whole crate is flagged under a synthetic ``<crate>`` key.
    """
    rs_files = [
        p for p in workspace.rglob("*.rs")
        if not any(part in _SKIP_DIRS for part in p.parts)
    ]
    total_tests = 0
    total_assertions = 0
    for p in rs_files:
        try:
            text = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        total_tests += len(_RUST_TEST_ATTR_RE.findall(text))
        total_assertions += len(_RUST_ASSERTION_RE.findall(text))
    if not rs_files:
        return {}  # nothing to judge — score_contract()'s job, not this one's
    if total_tests == 0:
        return {"<crate>": ("<no #[test] functions found>",)}
    if total_assertions == 0:
        return {"<crate>": ("<no assertion macros found>",)}
    return {}


def find_vacuous_js_test_file(test_file: Path) -> tuple[str, ...]:
    """A JS/TS test file is vacuous — judged as a WHOLE file, not per test
    case, since no JS parser is bundled here (unlike find_vacuous_test_functions'
    python AST walk) — if it registers ZERO test cases (no ``test()``/``it()``/
    ``describe()`` calls, the convention shared by node:test, Mocha, and
    Jest), or registers tests but contains no assertion call anywhere
    (``assert``/``expect``).

    Live incident this pins (2026-07-03, email validator goal): `test.js`
    was a bare ``process.exit(0)`` — zero test registrations, zero
    assertions — yet `npm test` exited 0 and the goal was marked complete.
    """
    try:
        text = test_file.read_text()
    except (OSError, UnicodeDecodeError):
        return ()
    if not _JS_TEST_REGISTRATION_RE.search(text):
        return ("<no test cases registered>",)
    if not _JS_ASSERTION_RE.search(text):
        return ("<no assertion calls found>",)
    return ()


def scan_workspace_for_vacuous_tests(workspace: Path, stack: str = "python") -> dict:
    """Map relative test-file path (or ``<crate>`` for rust's crate-wide
    check) -> vacuous marker(s). Python uses the AST walk above (per test
    function); node and rust use regex heuristics (coarser — whole-file for
    node, whole-crate for rust — since neither has a bundled parser here).
    cpp returns ``{}`` until an equivalent check is wired in."""
    results: dict = {}
    if stack == "python":
        for pattern in ("test_*.py", "*_test.py"):
            for p in workspace.rglob(pattern):
                if any(part in _SKIP_DIRS for part in p.parts):
                    continue
                rel = str(p.relative_to(workspace))
                if rel in results:
                    continue
                vacuous = find_vacuous_test_functions(p)
                if vacuous:
                    results[rel] = vacuous
        return results
    if stack == "node":
        seen: set = set()
        candidates = [
            p for pattern in _JS_TEST_GLOBS for p in workspace.rglob(pattern)
        ]
        for marker in _TEST_DIR_MARKERS:
            test_dir = workspace / marker
            if test_dir.is_dir():
                candidates += [p for p in test_dir.rglob("*") if p.suffix in (".js", ".ts")]
        for p in candidates:
            if p in seen or any(part in _SKIP_DIRS for part in p.parts):
                continue
            seen.add(p)
            rel = str(p.relative_to(workspace))
            if rel in results:
                continue
            vacuous = find_vacuous_js_test_file(p)
            if vacuous:
                results[rel] = vacuous
        return results
    if stack == "rust":
        return scan_rust_crate_for_vacuous_tests(workspace)
    return {}


# ---------------------------------------------------------------------------
# LLM-based overfitting review (complementary to everything above)
#
# The mutation gate and vacuous-assertion/registration gates are mechanical:
# they check whether a test's outcome depends on the source, or whether an
# assertion's truth value is a fixed constant. Neither can catch an
# implementation hardcoded to the EXACT literal inputs a test suite happens
# to use — e.g. `if x == "null": return None; raise ValueError(...)` — a
# hardcoded lookup table IS a real function call with a real non-constant
# assertion, structurally identical to genuine logic by every mechanical
# measure above. Distinguishing "genuine algorithm" from "overfitted to the
# visible tests" needs semantic judgment, not pattern matching — so this is
# an LLM review, deliberately using a DIFFERENT, stronger cloud model than
# whatever wrote the code, with fresh context and no attachment to its own
# prior reasoning. Language-agnostic by construction (no per-stack parser
# needed), unlike every other check in this module.
#
# Reproduced live (2026-07-04, JSON parser goal): the "parser" crashed on
# every real JSON input except the two literal strings from its own test
# file — mechanically indistinguishable from a real implementation to every
# check above, since the test's assertion genuinely depended on a real
# (if degenerate) function call.
# ---------------------------------------------------------------------------


_MAX_REVIEW_FILE_BYTES = 32_000


def _iter_text_files(workspace: Path, max_file_bytes: int = _MAX_REVIEW_FILE_BYTES):
    """Every reviewable text file under ``workspace`` — NO extension
    whitelist. Binary files are excluded by content sniff (NUL byte / decode
    failure), generated and VCS state by ``_SKIP_DIRS`` plus any dot-prefixed
    directory, oversized files by ``max_file_bytes``.

    This replaces per-stack glob lists as the root-cause fix for a whole
    class of reviewer-blindness bugs, each previously patched one at a time
    as it bit: implementations hidden in test-classified files (2026-07-05,
    email validator), workspaces with no test-classified file at all
    (2026-07-06, C++ postfix), build manifests invisible so the reviewer
    hallucinated "no CMake configuration" against passing cmake tests
    (2026-07-07, C++ postfix v3), and goal deliverables like results.csv
    that no source glob would ever match (2026-07-08, ML loop). Whitelists
    guarantee the NEXT stack or artifact type produces the same bug; "all
    text, minus generated/binary/oversized" cannot be blind to a file kind
    nobody predicted."""
    for p in sorted(workspace.rglob("*")):
        rel_parts = p.relative_to(workspace).parts
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts):
            continue
        if not p.is_file():
            continue
        try:
            if p.stat().st_size > max_file_bytes:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:1024]:
            continue  # binary
        try:
            yield p, raw.decode("utf-8")
        except UnicodeDecodeError:
            continue


def collect_review_content(workspace: Path, stack: str = "python", max_chars: int = 6000) -> tuple:
    """Concatenate non-test and test file contents (each independently
    budget-capped) for the LLM review prompts. Returns
    ``(source_text, test_text)``. ``stack`` is retained for call-site
    compatibility but no longer restricts which files are seen — see
    ``_iter_text_files``."""
    source_parts, test_parts = [], []
    source_budget = test_budget = max_chars
    for p, text in _iter_text_files(workspace):
        rel = str(p.relative_to(workspace))
        block = f"# {rel}\n{text}\n"
        if _is_test_file(p.relative_to(workspace)):
            if test_budget > 0:
                test_parts.append(block[:test_budget])
                test_budget -= len(block)
        else:
            if source_budget > 0:
                source_parts.append(block[:source_budget])
                source_budget -= len(block)
    # Co-located implementation: when the ONLY code in the workspace lives in
    # files classified as tests (e.g. `validateEmail` defined inside a bare
    # `test.js`), an empty source bucket would short-circuit both LLM review
    # gates before they ever saw the code. Reproduced live (2026-07-05, email
    # validator rerun): the "validator" was `email === 'test@example.com'` —
    # a pure hardcode — and the overfitting review silently skipped it
    # because source_text was empty. In that case review the test content AS
    # the implementation too, so hardcoding inside it is still visible.
    if not source_parts and test_parts:
        combined = "".join(test_parts)
        return combined, combined
    # Mirror case: no file classified as a TEST at all. Legitimate for the
    # CMake+CTest convention (`add_test(NAME x COMMAND the_same_binary)`) —
    # the "test" is just running the program and checking exit code/stdout,
    # not a separate test file `_is_test_file()` could ever recognize.
    # Reproduced live (2026-07-06, C++ postfix sandbox rerun): both semantic
    # gates silently returned `{}` before ever calling the LLM (test_text was
    # empty), so a `main.cpp` that printed "Test Passed" and exited 0 — with
    # zero postfix-evaluation logic anywhere, `evaluator.cpp` an unused stub
    # never linked into the build — was never reviewed at all. Without a
    # dedicated test file to separate from, review the source AS the test
    # too, so "no code attempts what the goal asked for" is still visible.
    if source_parts and not test_parts:
        combined = "".join(source_parts)
        return combined, combined
    return "".join(source_parts), "".join(test_parts)


# Output-discarding redirects: `>/dev/null`, `2>/dev/null`, `&>/dev/null`,
# `>/dev/null 2>&1`, `1>/dev/null`, `>> /dev/null` — anything that routes a
# stream into the void.
_DISCARD_REDIRECT_RE = re.compile(r"\s*(?:&>|[12]?>>?)\s*/dev/null(?:\s+2>&1)?")


def strip_output_discards(command: str) -> str:
    """Remove every output-discarding redirect from a shell command, so a
    FAILING command can be re-run once to recover the diagnostics its own
    plumbing threw away.

    This is the general, language-agnostic form of a failure class fixed
    piecemeal five times before (pipefail masking in python/rust T1s, `||`
    fallback masking in python T3 and run_tests/lint, `>/dev/null` discards
    in cpp T3/T4/T5): a test command whose failure path produces no evidence
    blinds every downstream consumer — the evaluator, the repair tasks, the
    steward, the human reading the log. Templates can be fixed one at a
    time forever; LLM-AUTHORED contract tests will keep reinventing the
    pattern. Fixing it at the consumption point (the evaluator re-runs any
    empty-output failure with discards stripped) covers every stack and
    every author, including future ones."""
    return _DISCARD_REDIRECT_RE.sub(" ", command or "")


_ARTIFACT_NAME_RE = re.compile(r"\b[\w./-]*\w\.[A-Za-z]{1,5}\b")
# Extension-looking tokens that are prose, not deliverables.
_ARTIFACT_STOP_EXT = {"e.g", "i.e", "etc", "vs"}
# Technology names that pattern-match a filename but never denote a file the
# goal wants on disk. Observed live (2026-07-10, Node CLI goal): "tracker in
# Node.js" made the artifact gate demand a file literally named `Node.js`,
# which can never exist — an unfixable permanent completion block. Matched
# case-insensitively against the whole token.
_ARTIFACT_STOP_NAMES = {
    "node.js", "vue.js", "react.js", "next.js", "nuxt.js", "angular.js",
    "ember.js", "backbone.js", "d3.js", "three.js", "chart.js", "alpine.js",
    "express.js", "p5.js", "socket.io", "asp.net", "vb.net", ".net",
}


def _goal_artifact_names(goal_text: str) -> list:
    """Concrete filenames the goal text itself names (results.csv,
    render.ppm, ...) — goal-driven, language-agnostic."""
    names = []
    for m in _ARTIFACT_NAME_RE.finditer(goal_text or ""):
        token = m.group(0)
        ext = token.rsplit(".", 1)[-1].lower()
        if ext in _ARTIFACT_STOP_EXT or token.lower() in _ARTIFACT_STOP_EXT:
            continue
        if token.lower() in _ARTIFACT_STOP_NAMES:
            continue
        if token not in names:
            names.append(token)
    return names[:10]


def _find_goal_artifact(workspace: Path, name: str):
    matches = [p for p in workspace.rglob(name.split("/")[-1])
               if not any(part in _SKIP_DIRS or part.startswith(".")
                          for part in p.relative_to(workspace).parts)]
    return matches[0] if matches else None


def missing_goal_artifacts(workspace: Path, goal_text: str) -> tuple:
    """Goal-named files that do NOT exist anywhere in the workspace — a
    DETERMINISTIC completion gate, not review context.

    Root cause this closes (2026-07-08, ML-loop v3, the incident that
    survived every LLM-review improvement): reviews judge whether a
    capability was ATTEMPTED — code that would produce results.csv counts —
    but the goal demanded the artifact EXIST, and it never did. Worse, the
    executor's test FABRICATED the artifact, asserted on its own
    fabrication, and deleted it (`df.to_csv(...); assert ...; os.remove()`)
    — self-satisfying evidence no semantic judgment reliably catches. File
    existence is not a judgment call; it must not be delegated to one."""
    return tuple(n for n in _goal_artifact_names(goal_text)
                 if _find_goal_artifact(workspace, n) is None)


def artifact_evidence(workspace: Path, goal_text: str) -> str:
    """A deterministic evidence block about every FILE the goal text itself
    names (results.csv, summary.md, render.ppm, ...): exists or MISSING,
    with size. Goal-driven and language-agnostic by construction — whatever
    deliverable the goal names is checked, whether or not any reviewer or
    template knows its file type.

    Root cause this closes (rather than the per-incident patches it
    replaces): reviews judged only workspace CODE, so a goal whose named
    deliverables simply didn't exist could still read as "all capabilities
    attempted" whenever the reviewer inferred intent from the code alone —
    reproduced live 2026-07-08 (ML loop: results.csv and summary.md never
    created) and 2026-07-07 (render.ppm existing but produced by a camera
    facing away from the scene; binary files are invisible to a text
    review, but existence+size at least anchors the judgment)."""
    names = _goal_artifact_names(goal_text)
    if not names:
        return ""
    lines = ["GOAL-NAMED ARTIFACTS (deterministic filesystem check — trust this over any code comment):"]
    for name in names:
        p = _find_goal_artifact(workspace, name)
        if p is not None:
            lines.append(f"- {name}: EXISTS ({p.stat().st_size} bytes at {p.relative_to(workspace)})")
        else:
            lines.append(f"- {name}: MISSING — no such file anywhere in the workspace")
    return "\n".join(lines)


def build_overfit_review_prompt(source_text: str, test_text: str, goal_title: str, goal_description: str) -> str:
    # `response_format: json_schema` is NOT reliably honored by every cloud
    # model behind Ollama's OpenAI-compat endpoint — verified live
    # (2026-07-04): gpt-oss:120b-cloud returned a fully correct, well-reasoned
    # analysis as pure markdown prose, ignoring the schema entirely. Ending
    # the prompt with an explicit, unambiguous "return ONLY this JSON shape"
    # instruction (matching engine/src/auditor.py's working pattern for its
    # own cloud-model prompts) is what actually gets compliant output.
    return (
        f"Goal: {goal_title}\n{goal_description}\n\n"
        "Below is a candidate implementation and its test suite. Determine whether the "
        "implementation is a genuine, general algorithm that would work correctly on "
        "realistic inputs beyond exactly what is tested — or whether it is hardcoded/"
        "overfitted to the specific test inputs (e.g. literal comparisons against exact "
        "test values, a lookup table keyed by test data, logic that provably cannot "
        "generalize to inputs the tests don't cover). Flag ONLY clear-cut cases; if "
        "genuinely unsure, do not flag it — a real but incomplete or simple "
        "implementation is not the same thing as a hardcoded one.\n\n"
        f"=== SOURCE ===\n{source_text}\n\n=== TESTS ===\n{test_text}\n\n"
        "Respond with ONLY a single JSON object in exactly this shape — no markdown, "
        "no code fences, no explanation before or after it:\n"
        '{"is_hardcoded": true|false, "reasoning": "...", "suspicious_snippets": ["..."]}'
    )


def review_for_hardcoded_implementation(
    workspace: Path,
    goal_title: str,
    goal_description: str,
    llm_review: Callable[[str], dict],
    stack: str = "python",
) -> dict:
    """Ask ``llm_review`` (injected for testability; production wiring passes
    a real cloud-model call) whether the implementation is hardcoded to the
    test suite rather than a genuine general algorithm.

    Returns ``{}`` if there's nothing to review or the model itself is
    unsure/says no. If the ``llm_review`` call ITSELF fails, returns
    ``{"review_error": "..."}`` so the caller can tell "reviewed and passed"
    from "review never happened" — the two were previously identical (both
    ``{}``), which let goals complete during cloud outages with zero trace in
    the logs (reproduced live 2026-07-05, Tic-Tac-Toe rerun: the scope gate
    silently no-opped and a suite testing 1 of 8 required win conditions
    shipped as "verified"). Otherwise returns the parsed verdict dict (at
    least ``is_hardcoded``, ``reasoning``, ``suspicious_snippets``).
    """
    source_text, test_text = collect_review_content(workspace, stack)
    if not source_text.strip() or not test_text.strip():
        return {}
    evidence = artifact_evidence(workspace, f"{goal_title} {goal_description}")
    if evidence:
        source_text = f"{evidence}\n\n{source_text}"
    prompt = build_overfit_review_prompt(source_text, test_text, goal_title, goal_description)
    try:
        result = llm_review(prompt)
    except Exception as e:
        return {"review_error": f"{type(e).__name__}: {str(e)[:300]}"}
    if not isinstance(result, dict) or not result.get("is_hardcoded"):
        return {}
    return result


# ---------------------------------------------------------------------------
# LLM-based scope-completeness review (same infrastructure as the
# overfitting review above, a different question)
#
# The deterministic per-stack templates in engine/src/auditor.py's
# template_contract() are intentionally GOAL-AGNOSTIC — "some source file
# compiles and some test suite passes" — by design, for reliability (a fixed
# template can't be tricked into a tech-mismatched or trivial contract the
# way an LLM-authored one could, per the comment in template_contract()
# itself). The tradeoff: nothing ties that check's SCOPE to what the goal
# actually asked for.
#
# Reproduced live (2026-07-04, Dijkstra goal): the goal explicitly asked for
# "Dijkstra's shortest path algorithm... returning shortest distance and
# path" with tests for "multiple paths, disconnected nodes, and a
# single-node graph". The loop instead built and independently-verified only
# a Graph data structure (itself genuine, non-hardcoded work — this is NOT
# an overfitting case) and stopped there, because the template contract
# never required Dijkstra's algorithm to exist at all. Every gate above
# targets fake/incomplete TESTS; this one targets an incomplete
# IMPLEMENTATION that legitimately passes genuine tests scoped too narrowly.
# ---------------------------------------------------------------------------


def build_scope_review_prompt(source_text: str, test_text: str, goal_title: str, goal_description: str) -> str:
    return (
        f"Goal: {goal_title}\n{goal_description}\n\n"
        "Below is the CURRENT implementation and test suite. Compare them against the FULL "
        "goal description above. The distinction that matters:\n"
        "  - A CAPABILITY the goal explicitly names (an algorithm, function, feature, or "
        "operation) that has NO code attempting it anywhere — zero lines of logic toward it — "
        "IS scope-incomplete. Flag it.\n"
        "  - A capability that IS implemented, even partially or imperfectly, is NOT "
        "scope-incomplete — this includes gaps in edge-case handling, incomplete test "
        "coverage, bugs, or missing polish for something that clearly exists in the code. "
        "Do NOT flag these, even if the goal used the word 'edge cases' — coverage gaps in an "
        "implemented feature are a test-quality concern, not a missing-capability concern.\n"
        "If genuinely unsure which case applies, do not flag it.\n\n"
        f"=== SOURCE ===\n{source_text}\n\n=== TESTS ===\n{test_text}\n\n"
        "Respond with ONLY a single JSON object in exactly this shape — no markdown, "
        "no code fences, no explanation before or after it:\n"
        '{"is_scope_incomplete": true|false, "reasoning": "...", "missing_requirements": ["..."]}'
    )


def review_for_goal_scope_completeness(
    workspace: Path,
    goal_title: str,
    goal_description: str,
    llm_review: Callable[[str], dict],
    stack: str = "python",
) -> dict:
    """Ask ``llm_review`` (injected for testability; production wiring passes
    a real cloud-model call) whether a core, explicitly-requested capability
    is completely missing from the implementation.

    Returns ``{}`` if there's nothing to review or the model itself is
    unsure/says everything requested was attempted — it never penalizes
    incompleteness of something that IS attempted (that's what the mechanical
    test gates are for). If the ``llm_review`` call ITSELF fails, returns
    ``{"review_error": "..."}`` so the caller can distinguish a clean pass
    from a review that never happened (see
    review_for_hardcoded_implementation for the live incident this fixed).
    Otherwise returns the parsed verdict dict (at least
    ``is_scope_incomplete``, ``reasoning``, ``missing_requirements``).
    """
    source_text, test_text = collect_review_content(workspace, stack)
    if not source_text.strip() or not test_text.strip():
        return {}
    evidence = artifact_evidence(workspace, f"{goal_title} {goal_description}")
    if evidence:
        source_text = f"{evidence}\n\n{source_text}"
    prompt = build_scope_review_prompt(source_text, test_text, goal_title, goal_description)
    try:
        result = llm_review(prompt)
    except Exception as e:
        return {"review_error": f"{type(e).__name__}: {str(e)[:300]}"}
    if not isinstance(result, dict) or not result.get("is_scope_incomplete"):
        return {}
    return result


# ---------------------------------------------------------------------------
# Semantic-flag memory: a durable record of workspace content the LLM review
# gates have already rejected.
#
# Reproduced live twice on the same day (2026-07-05, LRU-cache and
# JSON-parser reruns): a semantic gate blocks completion → the executor makes
# a genuine improvement (adds the missing tests) → the improvement regresses
# a mechanical audit test (a signature mismatch, a real bug the new tests
# exposed) → the vanishing-test guard auto-reverts the workspace to the LAST
# GOOD state — which is byte-for-byte the content the semantic gate had just
# rejected — and on the next all-green evaluation the goal completes, either
# because the cloud review call failed silently or because the model
# non-deterministically passed content it had flagged 9 consecutive times.
#
# The fix is memory, not more judgment: content-hash every review payload
# when a semantic gate blocks; at completion, identical content to a
# previously-flagged state is refused REGARDLESS of what (or whether) the
# reviewer answers this time. The hash changes the moment the executor
# actually addresses the flag, so a false positive costs one targeted edit,
# never a deadlock.
#
# Storage lives at ``<workspace>/.git/forge_semantic_flags.json`` — inside
# ``.git/`` deliberately: the vanishing-test guard's revert runs
# ``git checkout -- . && git clean -fd``, which wipes any untracked flag file
# in the worktree but never touches ``.git/`` itself, and it survives the
# cross-batch process restarts that reset in-memory AgentState.
# ---------------------------------------------------------------------------

_SEMANTIC_FLAGS_FILENAME = "forge_semantic_flags.json"


def review_content_hash(workspace: Path, stack: str = "python") -> str:
    """Stable hash of exactly what the LLM review gates would be shown.

    Returns "" when there is nothing reviewable (same condition under which
    the review functions themselves skip), so callers can guard on truthiness.
    """
    source_text, test_text = collect_review_content(workspace, stack)
    if not source_text.strip() or not test_text.strip():
        return ""
    return hashlib.sha256(
        f"{source_text}\x00{test_text}".encode("utf-8", errors="replace")
    ).hexdigest()


def _semantic_flags_path(workspace: Path) -> Path:
    return Path(workspace) / ".git" / _SEMANTIC_FLAGS_FILENAME


def load_semantic_flags(workspace: Path) -> dict:
    """Return {content_hash: reason} of previously-flagged review payloads."""
    import json
    path = _semantic_flags_path(workspace)
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_semantic_flag(workspace: Path, content_hash: str, reason: str) -> None:
    """Durably remember that ``content_hash`` was rejected, and why.

    No-ops (rather than erroring) when the workspace has no ``.git`` directory
    — without git there is also no revert machinery, so the failure mode this
    memory exists for cannot occur.
    """
    import json
    if not content_hash:
        return
    path = _semantic_flags_path(workspace)
    if not path.parent.is_dir():
        return
    flags = load_semantic_flags(workspace)
    flags[content_hash] = reason[:500]
    try:
        path.write_text(json.dumps(flags, indent=1))
    except OSError:
        pass
