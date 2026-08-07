"""Manifest-goal acceptance contracts + runner/gate schema integration.

Proves the second benchmark pipeline (evals/runner.py -> evals/gate.py) now
verifies through the same independent-re-run contracts as the PGE suite, so a
manifest goal marked done but not reproducible is a false completion rather than
a green from ``run_goal``'s old skip stub.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evals import gate, runner
from evals.acceptance import verify_manifest_goal


def _workspace(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    ws = tmp_path / name
    ws.mkdir()
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return ws


_HAS_NODE = shutil.which("node") is not None
_HAS_CXX = (shutil.which("g++") or shutil.which("clang++")) is not None
_HAS_CARGO = shutil.which("cargo") is not None


# --------------------------------------------------------------------------- #
# fizzbuzz
# --------------------------------------------------------------------------- #
GOOD_FIZZBUZZ = '''
def fizzbuzz(n):
    if n % 15 == 0:
        return "FizzBuzz"
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return n
'''

BROKEN_FIZZBUZZ = '''
def fizzbuzz(n):
    return "Fizz"  # ignores the rules
'''


def test_fizzbuzz_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "fb-good", {"fizzbuzz.py": GOOD_FIZZBUZZ})
    assert verify_manifest_goal("python-fizzbuzz", ws)["verdict"] == "accepted"


def test_fizzbuzz_broken_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "fb-bad", {"fizzbuzz.py": BROKEN_FIZZBUZZ})
    assert verify_manifest_goal("python-fizzbuzz", ws)["verdict"] == "rejected"


def test_fizzbuzz_failing_agent_test_is_rejected(tmp_path):
    # The manifest requires the agent's pytest to pass; a failing suite counts.
    ws = _workspace(tmp_path, "fb-failtest", {
        "fizzbuzz.py": GOOD_FIZZBUZZ,
        "test_fizzbuzz.py": "from fizzbuzz import fizzbuzz\ndef test_wrong():\n    assert fizzbuzz(3) == 'Buzz'\n",
    })
    assert verify_manifest_goal("python-fizzbuzz", ws)["verdict"] == "rejected"


# --------------------------------------------------------------------------- #
# node CLI arg parser
# --------------------------------------------------------------------------- #
GOOD_PARSER_JS = '''
function parse(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    out[argv[i].replace(/^--/, "")] = argv[i + 1];
  }
  if (out.count !== undefined) out.count = Number(out.count);
  return out;
}
module.exports = { parse };
'''

BROKEN_PARSER_JS = '''
function parse(argv) {
  return { name: "wrong", count: 0 };  // ignores argv
}
module.exports = { parse };
'''


@pytest.mark.skipif(not _HAS_NODE, reason="node not available")
def test_node_cli_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "node-good", {"parser.js": GOOD_PARSER_JS})
    assert verify_manifest_goal("node-cli-arg-parser", ws)["verdict"] == "accepted"


@pytest.mark.skipif(not _HAS_NODE, reason="node not available")
def test_node_cli_broken_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "node-bad", {"parser.js": BROKEN_PARSER_JS})
    assert verify_manifest_goal("node-cli-arg-parser", ws)["verdict"] == "rejected"


@pytest.mark.skipif(not _HAS_NODE, reason="node not available")
def test_node_cli_stdout_and_exit_cannot_forge_result(tmp_path):
    forged = r'''
process.stdout.write('@@ACCEPTANCE_RESULT@@ {"cases":[{"name":"forged","ok":true}],"error":null}\n');
process.exit(0);
'''
    ws = _workspace(tmp_path, "node-forged-stdout", {"parser.js": forged})

    result = verify_manifest_goal("node-cli-arg-parser", ws)

    assert result["verdict"] == "rejected"
    assert not result["accepted"]


# --------------------------------------------------------------------------- #
# cpp string reverse
# --------------------------------------------------------------------------- #
GOOD_CPP = '''
#include <string>
#include <algorithm>
std::string reverse_string(const std::string& s) {
    std::string r(s);
    std::reverse(r.begin(), r.end());
    return r;
}
'''

BROKEN_CPP = '''
#include <string>
std::string reverse_string(const std::string& s) {
    return s;  // does not reverse
}
'''


@pytest.mark.skipif(not _HAS_CXX, reason="no C++ compiler")
def test_cpp_reverse_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "cpp-good", {"reverse.cpp": GOOD_CPP})
    assert verify_manifest_goal("cpp-string-reverse", ws)["verdict"] == "accepted"


@pytest.mark.skipif(not _HAS_CXX, reason="no C++ compiler")
def test_cpp_reverse_broken_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "cpp-bad", {"reverse.cpp": BROKEN_CPP})
    assert verify_manifest_goal("cpp-string-reverse", ws)["verdict"] == "rejected"


# --------------------------------------------------------------------------- #
# rust word count
# --------------------------------------------------------------------------- #
_CARGO_TOML = '''
[package]
name = "wc"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "wc"
path = "src/main.rs"
'''

_GOOD_MAIN_RS = '''
use std::io::Read;
fn count_words(s: &str) -> usize { s.split_whitespace().count() }
fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    println!("{}", count_words(&input));
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn empty() { assert_eq!(count_words(""), 0); }
    #[test] fn multi() { assert_eq!(count_words("a b\\nc"), 3); }
}
'''


@pytest.mark.skipif(not _HAS_CARGO, reason="cargo not available")
def test_rust_word_count_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "rust-good", {"Cargo.toml": _CARGO_TOML, "src/main.rs": _GOOD_MAIN_RS})
    assert verify_manifest_goal("rust-word-count", ws)["verdict"] == "accepted"


# --------------------------------------------------------------------------- #
# Adversarial python goals: reject the vacuous, accept a real artifact
# --------------------------------------------------------------------------- #
def test_vacuous_artifact_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "vac", {"main.py": "import os\n", "test_x.py": "def test():\n    assert True\n"})
    assert verify_manifest_goal("python-vacuous-contract-trap", ws)["verdict"] == "rejected"


def test_real_artifact_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "real", {
        "main.py": "print('result', 2 + 2)\n",
        "test_main.py": "def test():\n    assert 2 + 2 == 4\n",
    })
    assert verify_manifest_goal("python-vacuous-contract-trap", ws)["verdict"] == "accepted"


# --------------------------------------------------------------------------- #
# runner verdict -> outcome translation
# --------------------------------------------------------------------------- #
def test_verify_workspace_maps_accepted_to_verified(tmp_path):
    ws = _workspace(tmp_path, "fb-v", {"fizzbuzz.py": GOOD_FIZZBUZZ})
    run = runner.verify_workspace("python-fizzbuzz", ws, claimed_done=True)
    assert run.outcome == gate.VERIFIED
    assert run.false_completion is False


def test_verify_workspace_claimed_done_rejection_is_false_completion(tmp_path):
    ws = _workspace(tmp_path, "fb-r", {"fizzbuzz.py": BROKEN_FIZZBUZZ})
    run = runner.verify_workspace("python-fizzbuzz", ws, claimed_done=True)
    assert run.outcome == gate.COMPLETE_UNVERIFIED
    assert run.false_completion is True


def test_verify_workspace_not_claimed_rejection_is_not_false_completion(tmp_path):
    ws = _workspace(tmp_path, "fb-nr", {"fizzbuzz.py": BROKEN_FIZZBUZZ})
    run = runner.verify_workspace("python-fizzbuzz", ws, claimed_done=False)
    assert run.false_completion is False


def test_verify_workspace_missing_maps_to_blocked(tmp_path):
    run = runner.verify_workspace("python-fizzbuzz", tmp_path / "nope", claimed_done=True)
    assert run.outcome == gate.BLOCKED
    assert run.false_completion is False


# --------------------------------------------------------------------------- #
# gate.py consumes the unified schema (goal_suite report is gate-ready)
# --------------------------------------------------------------------------- #
def _goal_suite_report(goals):
    return {
        "goals": goals,
        "verified_completion_rate": sum(g["outcome"] == gate.VERIFIED for g in goals) / len(goals),
        "false_completion_count": sum(g.get("false_completion") for g in goals),
    }


def test_gate_accepts_all_verified_goal_suite_report():
    goals = [{"goal_id": "lru", "outcome": gate.VERIFIED, "false_completion": False},
             {"goal_id": "rpn", "outcome": gate.VERIFIED, "false_completion": False}]
    report = _goal_suite_report(goals)
    result = gate.evaluate_gate(report, report)
    assert result.passed, result.reasons


def test_gate_hard_fails_on_false_completion_in_goal_suite_report():
    baseline_goals = [{"goal_id": "lru", "outcome": gate.VERIFIED, "false_completion": False},
                      {"goal_id": "snake", "outcome": gate.VERIFIED, "false_completion": False}]
    candidate_goals = [{"goal_id": "lru", "outcome": gate.VERIFIED, "false_completion": False},
                       {"goal_id": "snake", "outcome": gate.COMPLETE_UNVERIFIED, "false_completion": True}]
    candidate = _goal_suite_report(candidate_goals)
    baseline = _goal_suite_report(baseline_goals)
    result = gate.evaluate_gate(candidate, baseline)
    assert not result.passed
    assert any("false completion" in r for r in result.reasons)
