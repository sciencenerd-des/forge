"""Pins Phase 1 of specs/convergent-autonomous-harness.html: convergence ledger + controller."""
from forge_runtime.convergence import (
    STRATEGIES,
    ConvergenceController,
    TestOutcome,
    distance_to_done,
    progress_derivative,
)


def test_empty_contract_is_maximally_far():
    snap = distance_to_done([])
    assert snap.distance == 1.0
    assert snap.total == 0.0


def test_all_passing_is_zero_distance():
    results = [TestOutcome("t1", cycle=1, passed=True), TestOutcome("t2", cycle=1, passed=True)]
    snap = distance_to_done(results)
    assert snap.distance == 0.0
    assert snap.flaky_tests == ()


def test_latest_cycle_wins_per_test():
    results = [
        TestOutcome("t1", cycle=1, passed=False),
        TestOutcome("t1", cycle=2, passed=True),
    ]
    snap = distance_to_done(results)
    assert snap.distance == 0.0


def test_flaky_test_is_weighted_half_and_flagged():
    # alternates pass/fail/pass/fail -> 3 flips -> flaky
    results = [
        TestOutcome("flaky", cycle=1, passed=True),
        TestOutcome("flaky", cycle=2, passed=False),
        TestOutcome("flaky", cycle=3, passed=True),
        TestOutcome("flaky", cycle=4, passed=False),
    ]
    snap = distance_to_done(results)
    assert "flaky" in snap.flaky_tests
    assert snap.total == 0.5


def test_progress_derivative_needs_at_least_two_snapshots():
    assert progress_derivative([distance_to_done([])]) == 0.0


def test_progress_derivative_negative_means_improving():
    early = distance_to_done([TestOutcome("t1", 1, False)])
    late = distance_to_done([TestOutcome("t1", 1, False), TestOutcome("t1", 2, True)])
    d = progress_derivative([early, late])
    assert d < 0


def test_controller_only_ever_returns_mapped_strategies():
    ctl = ConvergenceController(epsilon=1.0)  # always explore
    snap = distance_to_done([TestOutcome("t1", 1, False)])
    for _ in range(50):
        decision = ctl.decide(snap, attempts=1)
        assert decision.action in STRATEGIES


def test_controller_blocks_after_attempt_budget_exhausted():
    ctl = ConvergenceController()
    snap = distance_to_done([TestOutcome("t1", 1, False)])
    decision = ctl.decide(snap, attempts=10)
    assert decision.action == "block"


def test_controller_retries_when_already_converged():
    ctl = ConvergenceController()
    snap = distance_to_done([TestOutcome("t1", 1, True)])
    decision = ctl.decide(snap, attempts=1)
    assert decision.action == "retry"


def test_controller_never_explores_into_block():
    ctl = ConvergenceController(epsilon=1.0)
    snap = distance_to_done([TestOutcome("t1", 1, False)])
    for _ in range(50):
        decision = ctl.decide(snap, attempts=1)
        assert decision.action != "block"


def test_controller_state_roundtrips():
    ctl = ConvergenceController()
    snap = distance_to_done([TestOutcome("t1", 1, False)])
    ctl.decide(snap, attempts=1)
    ctl.record_reward(-0.2)
    state = ctl.state()
    restored = ConvergenceController.from_state(state)
    assert restored.state() == state
