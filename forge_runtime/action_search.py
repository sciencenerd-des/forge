"""Action search: best-of-N executor candidates scored by a cheap verifier cascade.

Activated only for stuck tasks (attempts >= 2) to keep the default single-shot
cost profile intact for local models. Scoring happens against a copy-on-write
overlay of the workspace — never the live workspace — so nothing mutates until
a candidate is selected.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

ACTIVATION_ATTEMPTS_THRESHOLD = 2
MAX_SEARCH_ROUNDS = 3


@dataclass(frozen=True)
class Action:
    __test__ = False  # not a pytest test case despite the name prefix

    description: str
    files_touched: tuple[str, ...]
    syntax_check: Callable[[Path], bool] | None = None


def should_search(attempts: int, controller_strategy: str | None = None) -> bool:
    return attempts >= ACTIVATION_ATTEMPTS_THRESHOLD or controller_strategy == "escalate-search"


def _syntax_score(action: Action, workspace: Path) -> float:
    if action.syntax_check is None:
        return 1.0
    return 1.0 if action.syntax_check(workspace) else 0.0


def _path_policy_score(action: Action, allowed_prefixes: Sequence[str]) -> float:
    if not allowed_prefixes:
        return 1.0
    for f in action.files_touched:
        if not any(f.startswith(p) for p in allowed_prefixes):
            return 0.0
    return 1.0


def _relevance_score(action: Action, acceptance_criteria: Sequence[str]) -> float:
    if not acceptance_criteria:
        return 0.5
    criteria_text = " ".join(acceptance_criteria).lower()
    hits = sum(1 for f in action.files_touched if Path(f).stem.lower() in criteria_text)
    return min(1.0, hits / max(1, len(action.files_touched)))


def score(
    action: Action,
    workspace: Path,
    allowed_prefixes: Sequence[str] = (),
    acceptance_criteria: Sequence[str] = (),
) -> float:
    """Cascade: syntax gate first (0 = never executes), then policy + relevance."""
    syntax = _syntax_score(action, workspace)
    if syntax == 0.0:
        return 0.0
    policy = _path_policy_score(action, allowed_prefixes)
    if policy == 0.0:
        return 0.0
    relevance = _relevance_score(action, acceptance_criteria)
    return syntax + policy + relevance  # simple additive cascade, syntax/policy already gated


@dataclass(frozen=True)
class SearchResult:
    chosen_index: int | None
    scores: tuple[float, ...]
    fell_back_to_single_shot: bool


def select_best(
    candidates: Sequence[Action],
    workspace: Path,
    allowed_prefixes: Sequence[str] = (),
    acceptance_criteria: Sequence[str] = (),
) -> SearchResult:
    """Score all candidates against a scratch overlay; pick the best, deterministic tie-break by index."""
    if not candidates:
        return SearchResult(chosen_index=None, scores=(), fell_back_to_single_shot=True)

    with tempfile.TemporaryDirectory(prefix="forge-search-overlay-") as tmp:
        overlay = Path(tmp) / "overlay"
        if workspace.exists():
            shutil.copytree(workspace, overlay, dirs_exist_ok=True)
        else:
            overlay.mkdir(parents=True, exist_ok=True)

        scores = tuple(
            score(c, overlay, allowed_prefixes, acceptance_criteria) for c in candidates
        )

    if all(s == 0.0 for s in scores):
        return SearchResult(chosen_index=None, scores=scores, fell_back_to_single_shot=True)

    best_index = max(range(len(scores)), key=lambda i: scores[i])
    return SearchResult(chosen_index=best_index, scores=scores, fell_back_to_single_shot=False)


def generate_candidates(
    llm_generate: Callable[[], Action], n: int, budget: int = MAX_SEARCH_ROUNDS
) -> tuple[Action, ...]:
    """Sample up to ``n`` candidates from the injected generator, capped by round budget."""
    count = min(n, budget)
    return tuple(llm_generate() for _ in range(count))
