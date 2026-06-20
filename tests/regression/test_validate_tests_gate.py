"""Regression: the audit-test quality gate must not silently neuter a contract.

THE 2-week recurring failure: ``validate_tests`` silently dropped the
render-variety check (T7) — once because it matched the python-``import`` rule,
once because a compound ``if ...; then`` test's first token ``if`` looked like a
missing binary. With the variety check gone, a trivial build "passed" all
remaining tests, the evaluator declared the goal complete, and the loop exited
after one batch. These tests pin the gate's behavior so that can never recur.
"""
from src.auditor import detect_stack, template_contract, validate_tests

CPP = detect_stack("Brooklyn 3D Raytracer Development render raytracer")


def _ids(tests):
    return [t["id"] for t in tests]


def test_template_render_contract_survives_the_gate():
    """Every test the render template generates must pass validation — the
    contract may never be silently weakened at load time."""
    tmpl = template_contract(CPP, "Brooklyn 3D Raytracer render")
    kept, dropped = validate_tests(tmpl["tests"], CPP)
    assert dropped == [], f"template tests were dropped: {[(t['id'], t.get('rejected')) for t in dropped]}"
    assert "T7" in _ids(kept), "the render-variety check T7 must survive the gate"


def test_compound_shell_test_is_kept():
    """A compound ``if/then/fi`` test starts with a shell keyword, not a binary —
    the gate must not reject it as 'binary not found'."""
    t = {"id": "T7", "expect_substring": "VARIED",
         "command": "if head -c2 render.ppm | grep -q P6; then echo A; else echo B; fi; test 1 -gt 0 && echo VARIED"}
    kept, dropped = validate_tests([t], CPP)
    assert _ids(kept) == ["T7"], f"compound shell test wrongly dropped: {dropped}"


def test_legit_python_file_reader_is_kept():
    """A python test that READS a build artifact is legitimate verification and
    must survive — only bare module-existence probes are forbidden."""
    t = {"id": "X", "expect_substring": "",
         "command": "python3 -c \"d=open('render.ppm','rb').read(); exit(0 if len(d)>9 else 1)\""}
    kept, dropped = validate_tests([t], CPP)
    assert _ids(kept) == ["X"], f"legit python reader wrongly dropped: {dropped}"


def test_import_poison_is_still_dropped():
    """The actual poison — a bare ``python3 -c 'import <projmodule>'`` for a C++
    goal — must still be rejected."""
    t = {"id": "P", "expect_substring": "", "command": 'python3 -c "import raytracing"'}
    kept, dropped = validate_tests([t], CPP)
    assert _ids(dropped) == ["P"], "the import-raytracing poison must be dropped"


def test_genuinely_broken_tests_still_dropped():
    """The gate must still catch the real foot-guns it was built for."""
    bad = [
        {"id": "A", "command": "find . -name '*.cpp'", "expect_substring": ""},       # find always exits 0
        {"id": "B", "command": "pytest tests | grep PASS", "expect_substring": "P"},   # runner piped w/o 2>&1
        {"id": "C", "command": "echo hello", "expect_substring": ""},                  # no-op echo
        {"id": "D", "command": "definitely_not_a_real_binary --x", "expect_substring": "x"},
    ]
    kept, dropped = validate_tests(bad, CPP)
    assert set(_ids(dropped)) == {"A", "B", "C", "D"}, f"expected all dropped, got kept={_ids(kept)}"
