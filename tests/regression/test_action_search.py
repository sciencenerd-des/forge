"""Pins Phase 5 of specs/convergent-autonomous-harness.html: action search."""
from pathlib import Path

from forge_runtime.action_search import (
    Action,
    generate_candidates,
    select_best,
    should_search,
)


def test_should_search_only_after_two_attempts():
    assert not should_search(attempts=0)
    assert not should_search(attempts=1)
    assert should_search(attempts=2)


def test_should_search_on_controller_escalation_regardless_of_attempts():
    assert should_search(attempts=0, controller_strategy="escalate-search")


def test_candidate_failing_syntax_scores_zero_and_is_never_selected(tmp_path: Path):
    bad = Action("bad", ("a.py",), syntax_check=lambda ws: False)
    good = Action("good", ("b.py",), syntax_check=lambda ws: True)
    result = select_best([bad, good], tmp_path)
    assert result.chosen_index == 1
    assert result.scores[0] == 0.0


def test_all_candidates_failing_syntax_falls_back_to_single_shot(tmp_path: Path):
    bad1 = Action("bad1", ("a.py",), syntax_check=lambda ws: False)
    bad2 = Action("bad2", ("b.py",), syntax_check=lambda ws: False)
    result = select_best([bad1, bad2], tmp_path)
    assert result.fell_back_to_single_shot
    assert result.chosen_index is None


def test_tie_scores_pick_deterministic_first_index(tmp_path: Path):
    a = Action("a", ("x.py",))
    b = Action("b", ("y.py",))
    result = select_best([a, b], tmp_path)
    assert result.chosen_index == 0


def test_path_policy_zeroes_out_disallowed_candidate(tmp_path: Path):
    outside = Action("outside", ("/etc/passwd",))
    inside = Action("inside", ("src/main.py",))
    result = select_best([outside, inside], tmp_path, allowed_prefixes=("src/",))
    assert result.chosen_index == 1
    assert result.scores[0] == 0.0


def test_empty_candidates_falls_back(tmp_path: Path):
    result = select_best([], tmp_path)
    assert result.fell_back_to_single_shot
    assert result.chosen_index is None


def test_overlay_isolation_does_not_touch_live_workspace(tmp_path: Path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "keep.txt").write_text("original")

    def touch_workspace(ws: Path) -> bool:
        (ws / "mutated.txt").write_text("mutated")
        return True

    action = Action("touches overlay", ("keep.txt",), syntax_check=touch_workspace)
    select_best([action], live)
    assert not (live / "mutated.txt").exists()  # only the overlay was touched


def test_budget_caps_generated_candidates():
    counter = {"n": 0}

    def gen() -> Action:
        counter["n"] += 1
        return Action(f"c{counter['n']}", ())

    candidates = generate_candidates(gen, n=10, budget=3)
    assert len(candidates) == 3


def test_missing_workspace_is_handled_gracefully(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    action = Action("a", ("x.py",))
    result = select_best([action], missing)
    assert result.chosen_index == 0
