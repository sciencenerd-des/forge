"""Acceptance-contract tests.

These are real behavioral tests of the contracts themselves: each builds a
throwaway workspace containing a correct or a deliberately gamed solution and
asserts the contract accepts the former and rejects the latter. The gamed
fixtures are exactly the false completions the generic evaluator missed — a
Snake stub, a digits run with no accuracy — so a regression that reopens the
false-green hole fails here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.acceptance import isolate, verify_goal


def _workspace(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    ws = tmp_path / name
    ws.mkdir()
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return ws


# --------------------------------------------------------------------------- #
# LRU
# --------------------------------------------------------------------------- #
GOOD_LRU = '''
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
'''

# Never evicts: a capacity-ignoring dict masquerading as a cache.
BROKEN_LRU = '''
class LRUCache:
    def __init__(self, capacity):
        self.store = {}

    def get(self, key):
        return self.store.get(key, -1)

    def put(self, key, value):
        self.store[key] = value
'''


def test_lru_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "lru-good", {"lru_cache.py": GOOD_LRU})
    result = verify_goal("lru", ws)
    assert result["verdict"] == "accepted", result["reason"]


def test_lru_without_eviction_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "lru-bad", {"lru_cache.py": BROKEN_LRU})
    result = verify_goal("lru", ws)
    assert result["verdict"] == "rejected"
    assert not result["accepted"]


def test_lru_missing_file_is_unverifiable(tmp_path):
    ws = _workspace(tmp_path, "lru-empty", {"README.md": "todo"})
    result = verify_goal("lru", ws)
    assert result["verdict"] == "unverifiable"


# --------------------------------------------------------------------------- #
# RPN
# --------------------------------------------------------------------------- #
GOOD_RPN = '''
def eval_rpn(expr):
    stack = []
    for tok in expr.split():
        if tok in "+-*/":
            b = stack.pop(); a = stack.pop()
            stack.append({"+": a + b, "-": a - b, "*": a * b, "/": a / b}[tok])
        else:
            stack.append(float(tok))
    return stack[0]
'''

BROKEN_RPN = '''
def eval_rpn(expr):
    # Wrong: sums every number, ignores operators.
    return sum(float(t) for t in expr.split() if t not in "+-*/")
'''


def test_rpn_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "rpn-good", {"rpn_calculator.py": GOOD_RPN})
    result = verify_goal("rpn", ws)
    assert result["verdict"] == "accepted", result["reason"]


def test_rpn_wrong_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "rpn-bad", {"rpn_calculator.py": BROKEN_RPN})
    result = verify_goal("rpn", ws)
    assert result["verdict"] == "rejected"


# --------------------------------------------------------------------------- #
# JSON parser
# --------------------------------------------------------------------------- #
GOOD_JSON = '''
def parse(text):
    pos = 0

    def skip_ws():
        nonlocal pos
        while pos < len(text) and text[pos] in " \\t\\n\\r":
            pos += 1

    def value():
        nonlocal pos
        skip_ws()
        ch = text[pos]
        if ch == "{":
            return obj()
        if ch == "[":
            return arr()
        if ch == '"':
            return string()
        if text[pos:pos + 4] == "true":
            pos += 4; return True
        if text[pos:pos + 5] == "false":
            pos += 5; return False
        if text[pos:pos + 4] == "null":
            pos += 4; return None
        return number()

    def obj():
        nonlocal pos
        pos += 1; result = {}; skip_ws()
        if text[pos] == "}":
            pos += 1; return result
        while True:
            skip_ws(); key = string(); skip_ws(); pos += 1  # colon
            result[key] = value(); skip_ws()
            if text[pos] == ",":
                pos += 1; continue
            pos += 1; return result

    def arr():
        nonlocal pos
        pos += 1; result = []; skip_ws()
        if text[pos] == "]":
            pos += 1; return result
        while True:
            result.append(value()); skip_ws()
            if text[pos] == ",":
                pos += 1; continue
            pos += 1; return result

    def string():
        nonlocal pos
        pos += 1; chars = []
        while text[pos] != '"':
            chars.append(text[pos]); pos += 1
        pos += 1
        return "".join(chars)

    def number():
        nonlocal pos
        start = pos
        while pos < len(text) and text[pos] in "-+0123456789.eE":
            pos += 1
        token = text[start:pos]
        return int(token) if token.lstrip("-").isdigit() else float(token)

    return value()
'''

# Correct output but forbidden: leans on the stdlib json module.
CHEAT_JSON = '''
import json

def parse(text):
    return json.loads(text)
'''


def test_json_parser_from_scratch_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "json-good", {"json_parser.py": GOOD_JSON})
    result = verify_goal("json-parser", ws)
    assert result["verdict"] == "accepted", result["reason"]


def test_json_parser_importing_json_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "json-cheat", {"json_parser.py": CHEAT_JSON})
    result = verify_goal("json-parser", ws)
    assert result["verdict"] == "rejected"
    # The static "does not import json" check must be the failing one.
    assert any("does not import" in c["name"] and not c["passed"] for c in result["checks"])


# --------------------------------------------------------------------------- #
# Dijkstra
# --------------------------------------------------------------------------- #
GOOD_DIJKSTRA = '''
import heapq

def dijkstra(graph, source):
    dist = {node: float("inf") for node in graph}
    dist[source] = 0
    pq = [(0, source)]
    while pq:
        d, node = heapq.heappop(pq)
        if d > dist[node]:
            continue
        for nbr, weight in graph[node].items():
            nd = d + weight
            if nd < dist.get(nbr, float("inf")):
                dist[nbr] = nd
                heapq.heappush(pq, (nd, nbr))
    return dist
'''


def test_dijkstra_correct_is_accepted(tmp_path):
    ws = _workspace(
        tmp_path,
        "dijkstra-good",
        {
            "dijkstra.py": GOOD_DIJKSTRA,
            "test_dijkstra.py": "from dijkstra import dijkstra\nassert dijkstra({'A': {}}, 'A')['A'] == 0\n",
        },
    )
    result = verify_goal("dijkstra", ws)
    assert result["verdict"] == "accepted", result["reason"]


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


def test_dijkstra_tuple_adjacency_and_target_signature_is_accepted(tmp_path):
    ws = _workspace(
        tmp_path,
        "dijkstra-tuples",
        {
            "dijkstra.py": TUPLE_DIJKSTRA,
            "test_dijkstra.py": "from dijkstra import dijkstra\nassert callable(dijkstra)\n",
        },
    )

    result = verify_goal("dijkstra", ws)

    assert result["verdict"] == "accepted", result["reason"]


def test_dijkstra_without_useful_tests_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "dijkstra-no-tests", {"dijkstra.py": TUPLE_DIJKSTRA})

    result = verify_goal("dijkstra", ws)

    assert result["verdict"] == "rejected"
    assert "has a non-vacuous test assertion" in result["reason"]


def test_dijkstra_comment_and_string_asserts_are_not_tests(tmp_path):
    ws = _workspace(
        tmp_path,
        "dijkstra-fake-tests",
        {
            "dijkstra.py": TUPLE_DIJKSTRA,
            "test_dijkstra.py": (
                "# assert this implementation is useful\n"
                '"assert dijkstra(graph, source) == expected"\n'
            ),
        },
    )

    result = verify_goal("dijkstra", ws)

    assert result["verdict"] == "rejected"
    assert "has a non-vacuous test assertion" in result["reason"]


# --------------------------------------------------------------------------- #
# Tic-tac-toe
# --------------------------------------------------------------------------- #
GOOD_TTT = '''
def check_winner(board):
    lines = []
    for row in board:
        lines.append(row)
    for col in range(3):
        lines.append([board[r][col] for r in range(3)])
    lines.append([board[i][i] for i in range(3)])
    lines.append([board[i][2 - i] for i in range(3)])
    for line in lines:
        if line[0] != " " and line[0] == line[1] == line[2]:
            return line[0]
    return None
'''


def test_tic_tac_toe_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "ttt-good", {"tic_tac_toe.py": GOOD_TTT})
    result = verify_goal("tic-tac-toe", ws)
    assert result["verdict"] == "accepted", result["reason"]


def test_tic_tac_toe_enum_result_is_accepted(tmp_path):
    source = '''
from enum import Enum

class Player(Enum):
    X = "X"
    O = "O"

def check_winner(board):
    lines = list(board) + [[board[r][c] for r in range(3)] for c in range(3)]
    for line in lines:
        if line[0] != " " and line[0] == line[1] == line[2]:
            return Player(line[0])
    return None
'''
    ws = _workspace(tmp_path, "ttt-enum", {"tic_tac_toe.py": source})

    result = verify_goal("tic-tac-toe", ws)

    assert result["verdict"] == "accepted", result["reason"]


# --------------------------------------------------------------------------- #
# Digits — artifact content, not mere presence
# --------------------------------------------------------------------------- #
def _digits_writer(payload: str) -> str:
    return f'''
import json
# Stand-in for the training loop: what matters to the contract is the artifact.
with open("results.json", "w") as f:
    json.dump({payload}, f)
print("accuracy written")
'''


def test_digits_accuracy_above_threshold_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "digits-good", {"main.py": _digits_writer('{"accuracy": 0.97}')})
    result = verify_goal("digits", ws)
    assert result["verdict"] == "accepted", result["reason"]
    assert result["artifacts"]["accuracy"] == pytest.approx(0.97)


def test_digits_low_accuracy_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "digits-low", {"main.py": _digits_writer('{"accuracy": 0.5}')})
    result = verify_goal("digits", ws)
    assert result["verdict"] == "rejected"


def test_digits_results_without_accuracy_is_rejected(tmp_path):
    # This is the real false completion: a results.json that has shapes but no
    # accuracy, marked "completed" by the generic check.
    ws = _workspace(tmp_path, "digits-noacc",
                    {"main.py": _digits_writer('{"train_shape": [1437, 64], "test_shape": [360, 64]}')})
    result = verify_goal("digits", ws)
    assert result["verdict"] == "rejected"
    assert "accuracy" in result["reason"]


def test_digits_no_results_file_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "digits-none", {"main.py": 'print("did nothing")\n'})
    result = verify_goal("digits", ws)
    assert result["verdict"] == "rejected"


def test_digits_unshipped_import_is_an_artifact_defect_not_harness_error(tmp_path):
    # The agent imports a module it never shipped. With the verifier runtime
    # contract preflight-proven, that is the artifact's fault — the old blanket
    # "any ImportError -> harness error" un-counted false completions.
    ws = _workspace(tmp_path, "digits-unshipped",
                    {"main.py": "import helpers_that_do_not_exist_xyz\n"})
    result = verify_goal("digits", ws)
    assert result["verdict"] == "rejected"
    assert "artifact defect" in result["reason"]
    assert "helpers_that_do_not_exist_xyz" in result["reason"]


def test_classify_import_failure_attribution():
    from evals.acceptance import _classify_import_failure

    # A module the verifier image contractually provides -> harness fault.
    verdict, reason = _classify_import_failure(
        "Traceback...\nModuleNotFoundError: No module named 'numpy'")
    assert verdict == "error" and "preflight contract" in reason
    # Submodule of a contract package still attributes to the contract.
    verdict, _ = _classify_import_failure("No module named 'sklearn.datasets'")
    assert verdict == "error"
    # Anything outside the contract is the artifact's problem.
    verdict, reason = _classify_import_failure("No module named 'pandas'")
    assert verdict == "rejected" and "artifact defect" in reason
    # Module exists but the symbol/API is wrong -> agent bug, not runtime.
    verdict, reason = _classify_import_failure(
        "ImportError: cannot import name 'fit_all' from 'sklearn.ensemble'")
    assert verdict == "rejected"
    # No import failure at all -> caller falls back to plain rejection.
    assert _classify_import_failure("ZeroDivisionError: division by zero") is None


# --------------------------------------------------------------------------- #
# Snake — separate an input-driven loop from a placeholder
# --------------------------------------------------------------------------- #
GOOD_SNAKE = '''
import curses

def main(stdscr):
    curses.curs_set(0)
    snake = [(5, 5), (5, 4), (5, 3)]
    direction = (0, 1)
    food = (10, 10)
    while True:
        key = stdscr.getch()
        if key == curses.KEY_UP:
            direction = (-1, 0)
        elif key == curses.KEY_DOWN:
            direction = (1, 0)
        elif key == curses.KEY_LEFT:
            direction = (0, -1)
        elif key == curses.KEY_RIGHT:
            direction = (0, 1)
        head = (snake[0][0] + direction[0], snake[0][1] + direction[1])
        snake.insert(0, head)
        if head != food:
            snake.pop()
        stdscr.clear()
        for y, x in snake:
            stdscr.addch(y % 24, x % 80, "#")
        stdscr.addstr(0, 0, "score: %d" % len(snake))
        stdscr.refresh()

curses.wrapper(main)
'''

PLACEHOLDER_SNAKE = '''
import curses

def main(stdscr):
    curses.curs_set(0)
    stdscr.addstr(0, 0, "Snake Game Initialized")
    stdscr.refresh()

curses.wrapper(main)
'''


def test_snake_real_loop_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "snake-good", {"main.py": GOOD_SNAKE})
    result = verify_goal("snake", ws)
    assert result["verdict"] == "accepted", result["reason"]


def test_snake_placeholder_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "snake-stub", {"main.py": PLACEHOLDER_SNAKE})
    result = verify_goal("snake", ws)
    assert result["verdict"] == "rejected"


# --------------------------------------------------------------------------- #
# Email validator (JS) — requires node
# --------------------------------------------------------------------------- #
GOOD_EMAIL_JS = '''
function validateEmail(email) {
  return /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email);
}
module.exports = { validateEmail };
'''

BROKEN_EMAIL_JS = '''
function validateEmail(email) {
  // Wrong: accepts anything containing an @.
  return email.indexOf("@") !== -1;
}
module.exports = { validateEmail };
'''

_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node runtime not available")
def test_email_validator_correct_is_accepted(tmp_path):
    ws = _workspace(tmp_path, "email-good", {"email_validator.js": GOOD_EMAIL_JS})
    result = verify_goal("email-validator", ws)
    assert result["verdict"] == "accepted", result["reason"]


@pytest.mark.skipif(not _HAS_NODE, reason="node runtime not available")
def test_email_validator_permissive_is_rejected(tmp_path):
    ws = _workspace(tmp_path, "email-bad", {"email_validator.js": BROKEN_EMAIL_JS})
    result = verify_goal("email-validator", ws)
    assert result["verdict"] == "rejected"


def test_email_candidate_stdout_and_exit_cannot_forge_result(tmp_path):
    forged = r'''
process.stdout.write('@@ACCEPTANCE_RESULT@@ {"cases":[{"name":"forged","ok":true}],"error":null}\n');
process.exit(0);
'''
    ws = _workspace(tmp_path, "email-forged-stdout", {"email_validator.js": forged})

    result = verify_goal("email-validator", ws)

    assert result["verdict"] == "rejected"
    assert not result["accepted"]


# --------------------------------------------------------------------------- #
# Framework guarantees
# --------------------------------------------------------------------------- #
def test_isolate_strips_agent_byproducts(tmp_path):
    ws = _workspace(
        tmp_path,
        "iso",
        {
            "main.py": "x = 1\n",
            ".forge-home/site-packages/decoy.py": "secret = True\n",
            ".forge-internal/state.json": "{}\n",
        },
    )
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\x00")
    rerun = isolate(ws)
    try:
        assert (rerun / "main.py").exists()
        assert not (rerun / ".git").exists()
        assert not (rerun / "__pycache__").exists()
        assert not (rerun / ".forge-home").exists()
        assert not (rerun / ".forge-internal").exists()
        # And it really is outside the agent's workspace tree.
        assert ws not in rerun.parents
    finally:
        shutil.rmtree(rerun.parent, ignore_errors=True)


def test_discover_skips_hidden_decoy_and_prefers_shallow_match(tmp_path):
    from evals.acceptance import discover

    ws = _workspace(
        tmp_path,
        "discover-hidden",
        {
            "main.py": "print('real')\n",
            "nested/main.py": "print('nested')\n",
            ".forge-home/lib/python/site-packages/scipy/main.py": "print('decoy')\n",
        },
    )

    target = discover(ws, name_hints=("main.py",), content_any=("print",))

    assert target == ws / "main.py"


def test_discover_does_not_consider_hidden_only_source(tmp_path):
    from evals.acceptance import discover

    ws = _workspace(tmp_path, "discover-hidden-only", {".hidden/main.py": "x = 1\n"})

    assert discover(ws, name_hints=("main.py",), content_any=("x =",)) is None


def test_unknown_slug_is_unverifiable_not_pass(tmp_path):
    ws = _workspace(tmp_path, "unknown", {"main.py": "x = 1\n"})
    result = verify_goal("no-such-goal", ws)
    assert result["verdict"] == "unverifiable"
    assert not result["accepted"]


def test_missing_workspace_is_unverifiable_not_pass():
    result = verify_goal("lru", "/nonexistent/path/xyz")
    assert result["verdict"] == "unverifiable"
    assert not result["accepted"]


def test_failure_attribution_uses_deepest_traceback_frame():
    from evals.acceptance import _attribute_failure

    artifact_traceback = '''Traceback (most recent call last):
  File "/verify/_acceptance_driver.py", line 10, in <module>
    mod = _load(sys.argv[1])
  File "/verify/lexer.py", line 87
    elif char == '\\\\':
SyntaxError: unterminated string literal
'''
    driver_traceback = '''Traceback (most recent call last):
  File "/verify/agent.py", line 4, in check_winner
    return Player.X
  File "/verify/_acceptance_driver.py", line 20, in _emit
    json.dumps(payload)
TypeError: Object of type Player is not JSON serializable
'''

    assert _attribute_failure(artifact_traceback) == "artifact"
    assert _attribute_failure(driver_traceback) == "harness"
    assert _attribute_failure("docker: Error response from daemon") == "harness"
    assert _attribute_failure("") == "unknown"


def test_syntax_error_in_candidate_is_rejected_as_artifact_defect(tmp_path):
    ws = _workspace(
        tmp_path,
        "json-syntax-error",
        {"json_parser.py": "def parse(text):\n    elif text:\n        return text\n"},
    )

    result = verify_goal("json-parser", ws)

    assert result["verdict"] == "rejected"
    assert result["reason"].startswith("artifact defect:")


def test_missing_driver_result_with_docker_stderr_is_harness_error():
    from evals.acceptance import ContractResult, _cases_to_checks, _finalize

    proc = subprocess.CompletedProcess(
        args=["docker", "run"],
        returncode=127,
        stdout="",
        stderr='docker: Error response from daemon: exec: "/host/node": no such file',
    )
    checks, error = _cases_to_checks(["/host/node", "driver.js"], proc, None)

    result = _finalize(ContractResult(slug="email-validator"), checks, error)

    assert result.verdict == "error"
    assert result.reason.startswith("harness failure:")


def test_verdict_taxonomy_maps_to_suite_outcomes():
    from evals.orchestrator import _verdict_from

    rejected = _verdict_from({"verdict": "rejected"}, "completed", "completed", False)
    harness = _verdict_from({"verdict": "error"}, "completed", "completed", False)

    assert rejected == ("complete_unverified", True)
    assert harness == ("harness_error", False)


# --------------------------------------------------------------------------- #
# Phase 4: the execution runner is the security boundary
# --------------------------------------------------------------------------- #
def test_default_runner_is_host_and_non_gateable(tmp_path):
    ws = _workspace(tmp_path, "lru-host", {"lru_cache.py": GOOD_LRU})
    result = verify_goal("lru", ws)
    assert result["runner_label"] == "host-unsafe"
    assert result["gateable"] is False  # host execution can never gate a benchmark


def test_contract_commands_go_through_the_active_runner(tmp_path):
    # A custom runner records every command; if the contract bypassed the seam
    # (calling subprocess directly) this recorder would see nothing.
    import subprocess

    from evals.acceptance import HostRunner
    from evals.acceptance import verify_goal as vg

    seen = []

    class RecordingRunner(HostRunner):
        label = "recording"
        gateable = True

        def render(self, cmd, cwd):
            return ["recording-wrapper", *cmd]

        def run(self, cmd, cwd, *, timeout, stdin=None):
            seen.append(list(cmd))
            return subprocess.run(cmd, cwd=str(cwd), input=stdin, text=True,
                                  capture_output=True, timeout=timeout)

    ws = _workspace(tmp_path, "lru-seam", {"lru_cache.py": GOOD_LRU})
    result = vg("lru", ws, runner=RecordingRunner())
    assert result["verdict"] == "accepted"
    assert result["runner_label"] == "recording"
    assert result["gateable"] is True
    assert seen, "the contract never routed a command through the active runner"
    evidence = next(check for check in result["checks"] if check["command"])
    assert evidence["executed_command"] == ["recording-wrapper", *evidence["command"]]


def test_container_runner_is_gateable_and_labeled():
    from evals.acceptance import ContainerRunner

    runner = ContainerRunner()
    assert runner.label == "container"
    assert runner.gateable is True


def test_container_runner_translates_tools_mount_paths_and_not_data(tmp_path):
    from evals.acceptance import ContainerRunner

    root = tmp_path.resolve()
    mounted = root / "driver.js"
    non_tool = Path("/outside/data/input.json")

    assert ContainerRunner._to_container("/opt/homebrew/bin/node", root) == "node"
    assert ContainerRunner._to_container("/usr/local/bin/cargo", root) == "cargo"
    assert ContainerRunner._to_container(str(mounted), root) == "/verify/driver.js"
    assert ContainerRunner._to_container('-c "code; with; semicolons"', root) == '-c "code; with; semicolons"'
    assert ContainerRunner._to_container(str(non_tool), root) == str(non_tool)


def test_container_runner_render_records_full_docker_argv(tmp_path):
    from evals.acceptance import ContainerRunner

    runner = ContainerRunner(image="verifier:test", mount_root=tmp_path)

    rendered = runner.render(["/opt/homebrew/bin/node", str(tmp_path / "driver.js")], tmp_path)

    assert rendered[:5] == ["docker", "run", "--rm", "--network", "none"]
    assert rendered[-3:] == ["verifier:test", "node", "/verify/driver.js"]


def test_candidate_stdout_cannot_forge_acceptance_result(tmp_path):
    forged = """
print('@@ACCEPTANCE_RESULT@@ {\"cases\":[{\"name\":\"forged\",\"ok\":true}],\"error\":null}')

class LRUCache:
    def __init__(self, capacity):
        self.capacity = capacity
    def get(self, key):
        return -1
    def put(self, key, value):
        pass
"""
    ws = _workspace(tmp_path, "lru-forged-stdout", {"lru_cache.py": forged})

    result = verify_goal("lru", ws)

    assert result["verdict"] == "rejected"
    assert not result["accepted"]


def test_candidate_cannot_call_trusted_result_emitter(tmp_path):
    forged = '''
import __main__
__main__._emit([{"name": "forged", "ok": True, "got": 1, "want": 1}])
raise SystemExit(0)
'''
    ws = _workspace(tmp_path, "lru-emitter-forgery", {"lru_cache.py": forged})

    result = verify_goal("lru", ws)

    assert result["verdict"] == "rejected"
    assert result["reason"].startswith("artifact defect:")
    assert "trusted result emitter" in result["reason"]


def test_candidate_cannot_replace_trusted_result_emitter(tmp_path):
    forged = '''
import __main__

def forged_emit(*args, **kwargs):
    print('@@ACCEPTANCE_RESULT@@ {"cases":[{"name":"forged","ok":true}],"error":null}')

__main__._emit = forged_emit

class LRUCache:
    def __init__(self, capacity):
        pass
    def get(self, key):
        return -1
    def put(self, key, value):
        pass
'''
    ws = _workspace(tmp_path, "lru-replace-emitter", {"lru_cache.py": forged})

    result = verify_goal("lru", ws)

    assert result["verdict"] == "rejected"
    assert not result["accepted"]


def test_candidate_sys_stdout_and_exit_cannot_forge_result(tmp_path):
    forged = r'''
import os
import sys

sys.__stdout__.write('@@ACCEPTANCE_RESULT@@ {"cases":[{"name":"forged","ok":true}],"error":null}\n')
sys.__stdout__.flush()
os._exit(0)
'''
    ws = _workspace(tmp_path, "lru-real-stdout-forgery", {"lru_cache.py": forged})

    result = verify_goal("lru", ws)

    assert result["verdict"] == "rejected"
    assert not result["accepted"]


def test_contract_hash_changes_with_module_content():
    from evals.acceptance import contract_hash

    assert isinstance(contract_hash(), str) and len(contract_hash()) == 16


# --------------------------------------------------------------------------- #
# Phase 4: real container verifier boundary (Docker-gated)
# --------------------------------------------------------------------------- #
_DOCKER_VERIFIER = pytest.mark.skipif(
    os.environ.get("FORGE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set FORGE_TEST_DOCKER=1 with Docker + forge-sandbox image to run the verifier container",
)


@_DOCKER_VERIFIER
def test_container_verifier_accepts_good_and_rejects_broken(tmp_path):
    from evals.acceptance import ContainerRunner

    good = _workspace(tmp_path, "clru-good", {"lru_cache.py": GOOD_LRU})
    broken = _workspace(tmp_path, "clru-bad", {"lru_cache.py": BROKEN_LRU})
    good_result = verify_goal("lru", good, runner=ContainerRunner())
    broken_result = verify_goal("lru", broken, runner=ContainerRunner())
    assert good_result["verdict"] == "accepted", good_result["reason"]
    assert good_result["gateable"] is True and good_result["runner_label"] == "container"
    assert broken_result["verdict"] == "rejected"


@_DOCKER_VERIFIER
def test_container_verifier_translates_host_node_path(tmp_path):
    from evals.acceptance import ContainerRunner

    bare_export = GOOD_EMAIL_JS.replace("module.exports = { validateEmail };", "module.exports = validateEmail;")
    ws = _workspace(tmp_path, "container-email", {"email_validator.js": bare_export})

    result = verify_goal("email-validator", ws, runner=ContainerRunner())

    assert result["verdict"] == "accepted", result["reason"]
    evidence = next(check for check in result["checks"] if check["command"])
    assert evidence["command"][0].endswith("node")
    assert evidence["executed_command"][0:2] == ["docker", "run"]
    assert "node" in evidence["executed_command"]
