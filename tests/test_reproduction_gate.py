"""SWT-Bench-style fail-before/pass-after acceptance evidence."""

from __future__ import annotations

import sys
from pathlib import Path

from evals.acceptance import verify_goal

GOOD_LRU = """
from collections import OrderedDict

class LRUCache:
    def __init__(self, capacity):
        self.capacity = capacity
        self.store = OrderedDict()

    def get(self, key):
        if key not in self.store:
            return -1
        self.store.move_to_end(key)
        return self.store[key]

    def put(self, key, value):
        if key in self.store:
            self.store.move_to_end(key)
        self.store[key] = value
        if len(self.store) > self.capacity:
            self.store.popitem(last=False)
"""

BROKEN_LRU = """
class LRUCache:
    def __init__(self, capacity):
        self.store = {}

    def get(self, key):
        return self.store.get(key, -1)

    def put(self, key, value):
        self.store[key] = value
"""

REGRESSION_TEST = """
from lru_cache import LRUCache


def test_capacity_one_evicts_the_oldest_key():
    cache = LRUCache(1)
    cache.put("old", 1)
    cache.put("new", 2)
    assert cache.get("old") == -1
    assert cache.get("new") == 2
"""

NON_DISCRIMINATING_TEST = """
from lru_cache import LRUCache


def test_missing_key_returns_minus_one():
    assert LRUCache(1).get("missing") == -1
"""

FAILING_CANDIDATE_TEST = """
from lru_cache import LRUCache


def test_invented_behavior():
    assert LRUCache(1).get("missing") == 99
"""


def _workspace(tmp_path: Path, name: str, source: str, test: str | None = None) -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    (workspace / "lru_cache.py").write_text(source, encoding="utf-8")
    if test is not None:
        (workspace / "test_regression.py").write_text(test, encoding="utf-8")
    return workspace


def _spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "command": [sys.executable, "-m", "pytest", "-q", "test_regression.py"],
        "test_paths": ["test_regression.py"],
        "timeout_s": 30,
    }
    spec.update(overrides)
    return spec


def test_accepts_only_when_regression_test_fails_before_and_passes_after(tmp_path: Path):
    baseline = _workspace(tmp_path, "baseline", BROKEN_LRU)
    candidate = _workspace(tmp_path, "candidate", GOOD_LRU, REGRESSION_TEST)

    result = verify_goal(
        "lru",
        candidate,
        baseline_workspace=baseline,
        reproduction=_spec(),
    )

    assert result["verdict"] == "accepted", result["reason"]
    evidence = result["artifacts"]["reproduction"]
    assert evidence["status"] == "reproduced"
    assert evidence["baseline"]["passed"] is False
    assert evidence["candidate"]["passed"] is True
    assert evidence["coverage_delta"] is None
    assert "coverage_json not configured" in evidence["coverage_unavailable_reason"]


def test_rejects_test_that_already_passed_before_patch(tmp_path: Path):
    baseline = _workspace(tmp_path, "baseline", BROKEN_LRU)
    candidate = _workspace(tmp_path, "candidate", GOOD_LRU, NON_DISCRIMINATING_TEST)

    result = verify_goal(
        "lru",
        candidate,
        baseline_workspace=baseline,
        reproduction=_spec(),
    )

    assert result["verdict"] == "rejected"
    assert result["artifacts"]["reproduction"]["status"] == "not_reproduced"
    assert "also passed" in result["reason"]


def test_rejects_regression_test_that_fails_on_candidate(tmp_path: Path):
    baseline = _workspace(tmp_path, "baseline", BROKEN_LRU)
    candidate = _workspace(tmp_path, "candidate", GOOD_LRU, FAILING_CANDIDATE_TEST)

    result = verify_goal(
        "lru",
        candidate,
        baseline_workspace=baseline,
        reproduction=_spec(),
    )

    assert result["verdict"] == "rejected"
    evidence = result["artifacts"]["reproduction"]
    assert evidence["status"] == "candidate_failed"
    assert evidence["baseline"] is None


def test_records_coverage_delta_when_command_emits_coverage_json(tmp_path: Path):
    probe = """
import json
from pathlib import Path

fixed = "popitem(last=False)" in Path("lru_cache.py").read_text()
percent = 90.0 if fixed else 35.0
Path("coverage.json").write_text(json.dumps({"totals": {"percent_covered": percent}}))
raise SystemExit(0 if fixed else 1)
"""
    baseline = _workspace(tmp_path, "baseline", BROKEN_LRU)
    candidate = _workspace(tmp_path, "candidate", GOOD_LRU)
    (candidate / "reproduction_probe.py").write_text(probe, encoding="utf-8")

    result = verify_goal(
        "lru",
        candidate,
        baseline_workspace=baseline,
        reproduction={
            "command": [sys.executable, "reproduction_probe.py"],
            "test_paths": ["reproduction_probe.py"],
            "coverage_json": "coverage.json",
            "timeout_s": 30,
        },
    )

    evidence = result["artifacts"]["reproduction"]
    assert result["verdict"] == "accepted", result["reason"]
    assert evidence["baseline"]["coverage_percent"] == 35.0
    assert evidence["candidate"]["coverage_percent"] == 90.0
    assert evidence["coverage_delta"] == 55.0
    assert evidence["coverage_unavailable_reason"] is None


def test_reproduction_configuration_fails_closed(tmp_path: Path):
    candidate = _workspace(tmp_path, "candidate", GOOD_LRU, REGRESSION_TEST)

    result = verify_goal("lru", candidate, reproduction=_spec())

    assert result["verdict"] == "unverifiable"
    assert "must be provided together" in result["reason"]


def test_reproduction_test_path_cannot_escape_workspace(tmp_path: Path):
    baseline = _workspace(tmp_path, "baseline", BROKEN_LRU)
    candidate = _workspace(tmp_path, "candidate", GOOD_LRU, REGRESSION_TEST)

    result = verify_goal(
        "lru",
        candidate,
        baseline_workspace=baseline,
        reproduction=_spec(test_paths=["../test_regression.py"]),
    )

    assert result["verdict"] == "error"
    assert "escapes workspace" in result["reason"]
