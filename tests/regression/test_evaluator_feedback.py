"""Pins the evaluator's failure-feedback quality and the semantic revert
grace (engine/src/nodes/evaluator_node.py) — two halves of the same live
finding: the loop fails not because the model can't fix things, but because
the harness either hides the diagnosis or erases the attempt.

Incident A (2026-07-06/07, C++ postfix sandbox rerun): repair tasks carried
`current output: ''` for 32 straight attempts because the audit command
discarded diagnostics (fixed at the template level, pinned in
test_pipefail_hardening.py) — and even with output present, the description
kept only the LAST 200 chars, which for a cmake build is gmake trailer noise
(`*** Error 2`), never the compiler line naming the fix.

Incident B (2026-07-07, JSON parser sandbox rerun, 29+ batches): a perfect
three-state cycle — semantic gate rejects the primitives-only parser →
executor writes the fuller parser → new parser regresses T3 → ratchet
reverts BACK to the rejected parser → gate rejects it again. Reverting to
semantically-rejected content is provably pointless (the flag-memory gate
guarantees that content can never complete), so the ratchet now KEEPS the
forward attempt for one grace cycle when the revert target was just
semantically blocked.
"""

import pytest

pytest.importorskip("langgraph")
from src.nodes.evaluator_node import _failure_evidence, _semantic_revert_grace

# ---- _failure_evidence: what a repair task actually shows the executor ----

def test_empty_output_is_labeled_not_silently_quoted():
    evidence = _failure_evidence("")
    assert "NO output" in evidence
    assert "diagnostics" in evidence


def test_compiler_error_lines_beat_trailer_noise():
    # Real shape of a failed cmake build: the actionable line is in the
    # middle; the tail is useless gmake bookkeeping. A naive [-200:] slice
    # shows only the trailer.
    output = (
        "[ 25%] Building CXX object src/CMakeFiles/x.dir/x.cpp.o\n"
        "/w/src/PostfixEvaluator.hpp:4:32: error: 'string' in namespace 'std' does not name a type\n"
        "note: 'std::string' is defined in header '<string>'\n"
        + "gmake[2]: *** [src/CMakeFiles/x.dir/build.make:76: x.cpp.o] Error 1\n" * 10
        + "gmake[1]: *** [CMakeFiles/Makefile2:116: all] Error 2\n"
        "gmake: *** [Makefile:91: all] Error 2\n"
    )
    evidence = _failure_evidence(output)
    assert "does not name a type" in evidence, "the compiler diagnostic must survive truncation"


def test_output_without_error_lines_falls_back_to_tail():
    output = "\n".join(f"progress line {i}" for i in range(100))
    evidence = _failure_evidence(output, budget=100)
    assert "progress line 99" in evidence  # tail, not head


# ---- _semantic_revert_grace: when the ratchet must NOT revert ----

def test_grace_granted_when_previous_eval_was_a_semantic_block():
    assert _semantic_revert_grace({"semantic_block": True, "reason": "scope incomplete"}) is True


def test_no_grace_for_ordinary_regressions():
    # A regression following a normal failing evaluation reverts as always —
    # the grace exists ONLY for the transition out of a semantically-rejected
    # state, which is the only state reverting to which is provably futile.
    assert _semantic_revert_grace({"reason": "T3 failed"}) is False
    assert _semantic_revert_grace({}) is False
    assert _semantic_revert_grace(None) is False


def test_grace_is_self_limiting_one_cycle():
    # The grace return's own last_eval does NOT carry semantic_block (a
    # semantic block only happens on an all-green state), so two consecutive
    # grace cycles are impossible by construction: after one grace, the next
    # eval sees a non-semantic last_eval and the normal ratchet applies.
    grace_feedback = {"reason": "your new implementation is KEPT; fix the failures",
                      "missing_items": ["T3"]}
    assert _semantic_revert_grace(grace_feedback) is False


# ---- Review fallback chain + outage deferral (live incident 2026-07-08,
# ML experiment-loop goal): ollama.com went down (DNS failure) at exactly
# the final evaluation cycle — both semantic reviews failed open, the flag
# memory couldn't help because the executor's last edit produced a NEW
# never-reviewed hash, and a goal with NO results.csv, NO summary.md and NO
# training loop completed as "verified" on the goal-agnostic template tests
# alone. Reviewer downtime must not be a review bypass: reviews now fall
# back to the LOCAL model (always up while the loop runs at all), and if
# both fail, completion is DEFERRED (continue; retry next cycle) — never
# granted on never-reviewed content.

from src.nodes.evaluator_node import _review_call_with_fallback


class _DeadModel:
    def generate_json(self, *a, **k):
        raise ConnectionError("dial tcp: lookup ollama.com: no such host")


class _StubModel:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def generate_json(self, prompt, schema, max_tokens=1000):
        self.calls += 1
        return self.verdict


def test_cloud_outage_falls_back_to_the_local_model():
    local = _StubModel({"is_hardcoded": False, "reasoning": "local", "suspicious_snippets": []})
    result = _review_call_with_fallback("p", {}, 100, primary=_DeadModel(), fallback=local)
    assert result["reasoning"] == "local"
    assert local.calls == 1


def test_healthy_cloud_never_touches_the_fallback():
    cloud = _StubModel({"is_hardcoded": True, "reasoning": "cloud", "suspicious_snippets": []})
    local = _StubModel({"is_hardcoded": False, "reasoning": "local", "suspicious_snippets": []})
    result = _review_call_with_fallback("p", {}, 100, primary=cloud, fallback=local)
    assert result["reasoning"] == "cloud"
    assert local.calls == 0


def test_total_outage_raises_so_the_caller_defers_completion():
    import pytest as _pt
    with _pt.raises(Exception):
        _review_call_with_fallback("p", {}, 100, primary=_DeadModel(), fallback=_DeadModel())
