"""Pins the semantic-block thrash detector.

Live incident (2026-07-10, REST API task-manager goal): the goal required
empty-title POSTs to return 422. The executor repeatedly WEAKENED the test
(asserting 201) instead of adding validation; the scope gate correctly
blocked completion 11 times, but byte-fingerprint stagnation never advanced
— every weaken/revert flip changed the workspace — so the run thrashed for
over an hour with no terminating condition.

The first version of the detector compared verdict TEXT across batches and
never fired: the reviewer is an LLM and reworded the same verdict every
cycle ('ensuring that an empty title results in a 422' vs 'validating that
an empty title results in a 422'). The detector now counts ANY consecutive
semantic completion blocks — reaching the finish line and being semantically
blocked N batches in a row is non-convergence regardless of wording — and
stops the run honestly incomplete at PGE_MAX_SEMANTIC_BLOCKS.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from run_pge import _is_semantic_block


def test_not_a_block_without_semantic_block():
    assert not _is_semantic_block(None)
    assert not _is_semantic_block({})
    assert not _is_semantic_block({"reason": "tests failed"})
    assert not _is_semantic_block({"semantic_block": False, "missing_items": ["x"]})


def test_block_detected_regardless_of_wording():
    # The reviewer rewords its verdict every cycle; detection must not
    # depend on the reason text or missing_items being stable.
    a = {"semantic_block": True,
         "reason": "ensuring that an empty title results in a 422",
         "missing_items": ["a test case ensuring empty-title returns 422"]}
    b = {"semantic_block": True,
         "reason": "validating that an empty title results in a 422",
         "missing_items": ["validation that empty titles yield 422"]}
    assert _is_semantic_block(a) and _is_semantic_block(b)


def test_block_detected_with_no_items_or_reason():
    assert _is_semantic_block({"semantic_block": True})
