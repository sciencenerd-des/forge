"""Pins the three CLASS-level fixes that replace per-incident patches
(2026-07-08). Each class had produced 3-5 separate live incidents, each
patched at its site; these mechanisms close the class so the next stack,
goal shape, or contract author cannot reproduce it.

Class A — contracts that destroy their own failure evidence
  (pipefail masking ×2, `||` fallback masking ×2, `>/dev/null` discard ×3):
  strip_output_discards + the evaluator's empty-output-failure re-run.
Class B — reviewers judging an extension-whitelisted keyhole view
  (co-located tests, no-test-file, invisible CMakeLists, invisible
  results.csv): _iter_text_files — ALL text files, no whitelist.
Class C — goal-named deliverables invisible to judgment
  (missing results.csv/summary.md completing "verified"): artifact_evidence.
"""
from pathlib import Path

from forge_runtime.contract_immune import (
    artifact_evidence,
    collect_review_content,
    strip_output_discards,
)

# ---- Class A: strip_output_discards ----------------------------------------

def test_strips_the_exact_cpp_template_discard_pattern():
    cmd = "cmake --build build >/dev/null 2>&1 && echo BUILD_OK"
    bare = strip_output_discards(cmd)
    assert "/dev/null" not in bare
    assert "cmake --build build" in bare and "echo BUILD_OK" in bare


def test_strips_every_discard_variant():
    for variant in (">/dev/null", "2>/dev/null", "&>/dev/null",
                    ">/dev/null 2>&1", "1>/dev/null", ">> /dev/null"):
        assert "/dev/null" not in strip_output_discards(f"tool --arg {variant} && echo OK")


def test_leaves_non_discarding_commands_untouched():
    for cmd in ("pytest -q 2>&1 | tail -3",
                "cmake --build build > build.log 2>&1",
                "echo hello"):
        assert strip_output_discards(cmd) == cmd


def test_recovered_command_actually_surfaces_the_error(tmp_path):
    # End to end: the blinded form produces nothing; the stripped form
    # produces the actionable diagnostic — exactly the evaluator's re-run.
    import subprocess
    blind = "ls /definitely/not/a/path >/dev/null 2>&1 && echo OK"
    r1 = subprocess.run(["/bin/bash", "-lc", blind], capture_output=True, text=True, cwd=tmp_path)
    assert r1.returncode != 0 and not (r1.stdout + r1.stderr).strip()
    r2 = subprocess.run(["/bin/bash", "-lc", strip_output_discards(blind)],
                        capture_output=True, text=True, cwd=tmp_path)
    assert "No such file or directory" in (r2.stdout + r2.stderr)


# ---- Class B: whitelist-free review content ---------------------------------

def test_review_sees_file_types_no_stack_glob_ever_listed(tmp_path: Path):
    # A stack nobody wrote a glob for (Go) + a config format (yaml) + a doc.
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    (tmp_path / "config.yaml").write_text("threshold: 0.95\n")
    (tmp_path / "NOTES.md").write_text("design notes\n")
    source, _ = collect_review_content(tmp_path, stack="python")
    assert "package main" in source
    assert "threshold: 0.95" in source
    assert "design notes" in source


def test_review_still_excludes_binaries_generated_dirs_and_oversized(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    build = tmp_path / "build"
    build.mkdir()
    (build / "generated.py").write_text("must_not_appear = True\n")
    (tmp_path / "huge.txt").write_text("A" * 200_000)
    source, tests = collect_review_content(tmp_path, stack="python")
    combined = source + tests
    assert "x = 1" in combined
    assert "must_not_appear" not in combined
    assert "A" * 1000 not in combined


def test_text_artifacts_named_by_goals_are_now_visible_in_review(tmp_path: Path):
    # The ML-loop shape: results.csv is a deliverable no source glob matched.
    (tmp_path / "train.py").write_text("print('training')\n")
    (tmp_path / "results.csv").write_text("run,param,accuracy\n1,rbf,0.97\n")
    source, _ = collect_review_content(tmp_path, stack="python")
    assert "run,param,accuracy" in source


# ---- Class C: artifact_evidence ----------------------------------------------

GOAL = ("train a classifier, logging runs to results.csv, and write a "
        "summary.md naming the best configuration; render to output.ppm")


def test_missing_goal_named_artifacts_are_reported_missing(tmp_path: Path):
    evidence = artifact_evidence(tmp_path, GOAL)
    assert "results.csv: MISSING" in evidence
    assert "summary.md: MISSING" in evidence
    assert "output.ppm: MISSING" in evidence


def test_existing_goal_named_artifacts_are_reported_with_size(tmp_path: Path):
    (tmp_path / "results.csv").write_text("run,acc\n1,0.97\n")
    evidence = artifact_evidence(tmp_path, GOAL)
    assert "results.csv: EXISTS (15 bytes" in evidence
    assert "summary.md: MISSING" in evidence


def test_prose_dots_are_not_mistaken_for_artifacts(tmp_path: Path):
    evidence = artifact_evidence(tmp_path, "improve accuracy, e.g. tune params etc. properly")
    assert "e.g" not in evidence
    assert "etc" not in evidence


def test_goals_naming_no_files_produce_no_evidence_block(tmp_path: Path):
    assert artifact_evidence(tmp_path, "implement quicksort with tests") == ""


def test_technology_names_are_not_mistaken_for_artifacts(tmp_path: Path):
    # Live incident (2026-07-10, Node CLI goal): "tracker in Node.js" made the
    # artifact gate demand a file literally named Node.js — a permanent,
    # unfixable completion block. Tech names that look like filenames must be
    # skipped while real deliverables in the same goal are still checked.
    goal = "Build a CLI expense tracker in Node.js persisting to expenses.json with a Vue.js dashboard"
    evidence = artifact_evidence(tmp_path, goal)
    assert "Node.js" not in evidence
    assert "Vue.js" not in evidence
    assert "expenses.json: MISSING" in evidence


# ---- Class C, deterministic half: missing_goal_artifacts as a completion
# gate. Live incident (2026-07-08, ML loop v3 — survived every LLM-review
# improvement): the goal named results.csv and summary.md; code that WOULD
# produce them existed, so the scope review counted the capability as
# attempted — but the files never did. The executor's test even FABRICATED
# results.csv, asserted on its own fabrication, and deleted it
# (`df.to_csv(...); assert ...; os.remove(...)`). File existence is not a
# judgment call and is no longer delegated to one.

from forge_runtime.contract_immune import missing_goal_artifacts


def test_missing_deliverables_are_detected(tmp_path: Path):
    missing = missing_goal_artifacts(tmp_path, GOAL)
    assert "results.csv" in missing
    assert "summary.md" in missing


def test_present_deliverables_are_not_flagged(tmp_path: Path):
    (tmp_path / "results.csv").write_text("run,acc\n1,0.97\n")
    (tmp_path / "summary.md").write_text("# best config\n")
    (tmp_path / "output.ppm").write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
    assert missing_goal_artifacts(tmp_path, GOAL) == ()


def test_deliverables_in_subdirectories_count(tmp_path: Path):
    sub = tmp_path / "out"
    sub.mkdir()
    (sub / "results.csv").write_text("run,acc\n")
    missing = missing_goal_artifacts(tmp_path, GOAL)
    assert "results.csv" not in missing


def test_goals_without_filenames_never_block(tmp_path: Path):
    assert missing_goal_artifacts(tmp_path, "implement quicksort with pytest tests") == ()


def test_the_fabricate_assert_delete_pattern_is_caught(tmp_path: Path):
    # The test file exists (and fabricates the artifact transiently), but the
    # artifact does NOT persist in the workspace — exactly the live incident.
    (tmp_path / "test_ml.py").write_text(
        "import pandas as pd, os\n"
        "def test_csv():\n"
        "    pd.DataFrame({'acc': [0.96]}).to_csv('results.csv')\n"
        "    assert os.path.exists('results.csv')\n"
        "    os.remove('results.csv')\n"
    )
    assert "results.csv" in missing_goal_artifacts(tmp_path, GOAL)
