"""Replay fixtures distilled from the July 15 acceptance misjudgments."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from evals.acceptance import ContainerRunner, verify_goal


def _workspace(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    workspace = tmp_path / name
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return workspace


ENUM_TIC_TAC_TOE = '''
from enum import Enum

class Player(Enum):
    X = "X"
    O = "O"

def check_winner(board):
    lines = list(board) + [[board[row][column] for row in range(3)] for column in range(3)]
    for line in lines:
        if line[0] != " " and line[0] == line[1] == line[2]:
            return Player(line[0])
    return None
'''

TUPLE_DIJKSTRA = '''
import heapq

def dijkstra(graph, source, end):
    distances = {source: 0}
    previous = {}
    queue = [(0, source)]
    while queue:
        distance, node = heapq.heappop(queue)
        if node == end:
            path = []
            while node in previous:
                path.append(node)
                node = previous[node]
            return distance, [source, *reversed(path)]
        for neighbor, weight in graph[node]:
            candidate = distance + weight
            if candidate < distances.get(neighbor, float("inf")):
                distances[neighbor] = candidate
                previous[neighbor] = node
                heapq.heappush(queue, (candidate, neighbor))
    return float("inf"), []
'''

BARE_EMAIL_VALIDATOR = r'''
function validateEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}
module.exports = validateEmail;
'''


def test_recorded_enum_tic_tac_toe_is_accepted(tmp_path):
    workspace = _workspace(tmp_path, "enum-ttt", {"tic_tac_toe.py": ENUM_TIC_TAC_TOE})

    result = verify_goal("tic-tac-toe", workspace)

    assert result["verdict"] == "accepted", result["reason"]
    assert result["reason"].startswith("all authoritative checks passed")


def test_recorded_tuple_dijkstra_with_tests_is_accepted(tmp_path):
    workspace = _workspace(
        tmp_path,
        "tuple-dijkstra-tested",
        {
            "dijkstra.py": TUPLE_DIJKSTRA,
            "test_dijkstra.py": "from dijkstra import dijkstra\nassert callable(dijkstra)\n",
        },
    )

    result = verify_goal("dijkstra", workspace)

    assert result["verdict"] == "accepted", result["reason"]
    assert result["reason"].startswith("all authoritative checks passed")


def test_recorded_tuple_dijkstra_without_tests_is_rejected(tmp_path):
    workspace = _workspace(tmp_path, "tuple-dijkstra-untested", {"dijkstra.py": TUPLE_DIJKSTRA})

    result = verify_goal("dijkstra", workspace)

    assert result["verdict"] == "rejected"
    assert result["reason"] == "failed checks: has a non-vacuous test assertion"


def test_recorded_digits_stub_ignores_forge_home_decoy_and_is_rejected(tmp_path):
    workspace = _workspace(
        tmp_path,
        "digits-decoy",
        {
            "main.py": "print('training not implemented')\n",
            ".forge-home/lib/python3.11/site-packages/scipy/_lib/cobyqa/main.py": (
                "import json\njson.dump({'accuracy': 1.0}, open('results.json', 'w'))\n"
            ),
        },
    )

    result = verify_goal("digits", workspace)

    assert result["verdict"] == "rejected"
    assert result["reason"] == "no results.json produced by the training run"
    assert result["target_file"].endswith("/main.py")
    assert ".forge-home" not in result["target_file"]


def test_recorded_json_syntax_error_is_rejected_as_artifact_defect(tmp_path):
    workspace = _workspace(
        tmp_path,
        "json-syntax-error",
        {"lexer.py": "def parse(text):\n    elif text:\n        return text\n"},
    )

    result = verify_goal("json-parser", workspace)

    assert result["verdict"] == "rejected"
    assert result["reason"].startswith("artifact defect:")


_DOCKER = pytest.mark.skipif(
    os.environ.get("FORGE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set FORGE_TEST_DOCKER=1 to replay the email-validator container boundary",
)


@_DOCKER
def test_recorded_bare_export_email_validator_is_accepted_in_container(tmp_path):
    workspace = _workspace(
        tmp_path,
        "bare-email-validator",
        {"email_validator.js": BARE_EMAIL_VALIDATOR},
    )

    result = verify_goal("email-validator", workspace, runner=ContainerRunner())

    assert result["verdict"] == "accepted", result["reason"]
    assert result["reason"].startswith("all authoritative checks passed")
