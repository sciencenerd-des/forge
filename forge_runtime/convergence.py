"""Convergence ledger and controller.

Turns the evaluator's binary verdict into a dense, persisted progress signal
and replaces reactive stagnation handling with an informed controller.

Distance-to-done: D(t) = 1 - (weighted tests passing / total contract tests).
A test that has alternated pass/fail at least twice is a *flake* and is
weighted 0.5 instead of 1.0 so it can't single-handedly stall convergence.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TestOutcome:
    __test__ = False  # not a pytest test case despite the name prefix

    test_id: str
    cycle: int
    passed: bool


@dataclass(frozen=True)
class LedgerSnapshot:
    distance: float
    passing: float
    total: float
    flaky_tests: tuple[str, ...]


# Controller strategies. Keep this list in sync with the graph's mapped edges
# (Lesson 4: a router must only ever return a real edge) — every value here
# must correspond to something the caller knows how to route.
STRATEGIES = ("retry", "re-decompose", "escalate-context", "switch-provider-profile", "block")


def _is_flaky(results: Sequence[TestOutcome]) -> bool:
    """A test flakes if its pass/fail value changes at least twice across cycles."""
    ordered = sorted(results, key=lambda r: r.cycle)
    flips = sum(
        1 for a, b in zip(ordered, ordered[1:]) if a.passed != b.passed
    )
    return flips >= 2


def distance_to_done(results: Iterable[TestOutcome]) -> LedgerSnapshot:
    """Compute weighted distance-to-done from a flat list of per-test results.

    Only the latest result per test_id counts toward pass/fail; the full
    history for that test_id is used to detect flakiness.
    """
    by_test: dict[str, list[TestOutcome]] = {}
    for r in results:
        by_test.setdefault(r.test_id, []).append(r)

    if not by_test:
        # No contract tests recorded yet == maximally far from done.
        return LedgerSnapshot(distance=1.0, passing=0.0, total=0.0, flaky_tests=())

    passing = 0.0
    total = 0.0
    flaky: list[str] = []
    for test_id, history in by_test.items():
        latest = max(history, key=lambda r: r.cycle)
        weight = 1.0
        if _is_flaky(history):
            weight = 0.5
            flaky.append(test_id)
        total += weight
        if latest.passed:
            passing += weight

    distance = 1.0 - (passing / total if total else 0.0)
    return LedgerSnapshot(distance=distance, passing=passing, total=total, flaky_tests=tuple(sorted(flaky)))


def progress_derivative(snapshots: Sequence[LedgerSnapshot]) -> float:
    """ΔD over a sliding window: negative means improving (distance shrinking)."""
    if len(snapshots) < 2:
        return 0.0
    return snapshots[-1].distance - snapshots[0].distance


@dataclass
class SteeringDecision:
    action: str
    reason: str


@dataclass
class ConvergenceController:
    """Epsilon-greedy bandit over recovery strategies.

    Reward for the arm last chosen is the *negative* subsequent ΔD (a bigger
    drop in distance is a better reward). Arm statistics persist on the
    instance so a caller can serialize/restore them across a resumed run.
    """

    epsilon: float = 0.2
    rng: random.Random = field(default_factory=random.Random)
    _counts: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STRATEGIES})
    _values: dict[str, float] = field(default_factory=lambda: {s: 0.0 for s in STRATEGIES})
    _last_arm: str | None = None

    def record_reward(self, derivative: float) -> None:
        """Feed back the ΔD observed after the last decision was acted on."""
        if self._last_arm is None:
            return
        reward = -derivative
        n = self._counts[self._last_arm] + 1
        self._counts[self._last_arm] = n
        old = self._values[self._last_arm]
        self._values[self._last_arm] = old + (reward - old) / n

    def decide(self, snapshot: LedgerSnapshot, attempts: int) -> SteeringDecision:
        """Choose a strategy. Always returns one of STRATEGIES (a mapped edge)."""
        if snapshot.total == 0.0:
            decision = SteeringDecision("retry", "no contract tests observed yet")
        elif snapshot.distance <= 0.0:
            decision = SteeringDecision("retry", "already converged; nothing to steer")
        elif attempts >= 6:
            decision = SteeringDecision("block", "attempt budget exhausted with no convergence")
        elif self.rng.random() < self.epsilon or not any(self._counts.values()):
            arm = self.rng.choice(STRATEGIES[:-1])  # never explore into "block"
            decision = SteeringDecision(arm, "exploration")
        else:
            arm = max(STRATEGIES[:-1], key=lambda s: self._values[s])
            decision = SteeringDecision(arm, f"best known arm (value={self._values[arm]:.3f})")

        self._last_arm = decision.action
        return decision

    def state(self) -> dict:
        """Serializable snapshot of bandit arm statistics for persistence/resume."""
        return {"counts": dict(self._counts), "values": dict(self._values)}

    @classmethod
    def from_state(cls, state: dict, epsilon: float = 0.2) -> "ConvergenceController":
        ctl = cls(epsilon=epsilon)
        ctl._counts.update(state.get("counts", {}))
        ctl._values.update(state.get("values", {}))
        return ctl
