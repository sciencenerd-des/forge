"""Per-goal acceptance contracts for the eight-goal PGE suite.

The launcher/subprocess exit code and the durable DB rows (goal status, a test
row that reports "success", a file-change count) only tell us that the agent
*claimed* to finish. They are trivially satisfied by a stub: a Snake "game"
whose only test is ``assert True``, or a digits ``main.py`` that writes a
``results.json`` with no accuracy in it, both register as "completed" +
"success" under the generic check. That is a false completion — the reward
channel lied.

This module replaces the generic check with a *contract* per goal. A contract:

1. **Re-runs outside the agent's own sandbox.** The workspace is copied to a
   fresh temp dir (``.git``/``__pycache__``/caches stripped), so we never
   observe agent-produced ``.pyc``, a live process, or state that only exists
   inside ``~/.forge/workspaces``. If the code isn't self-contained, it fails
   here.
2. **Locates the required files** by content signature, not a hard-coded name,
   because agents name things differently across runs.
3. **Runs its own authoritative tests**, not the agent's. We inject a driver,
   invoke it with an **exact command**, and **capture stdout/stderr**. Pass/fail
   is decided from structured driver output, never from exit code alone — a
   process that ``sys.exit(0)`` without doing the work does not pass.
4. **Inspects artifact contents** — e.g. digits must leave a ``results.json``
   whose accuracy is numeric and > 0.95, reproduced by our re-run, not merely
   present.

A contract returns one of four verdicts. Only ``accepted`` is a pass:

* ``accepted``     — reproduced and behaves correctly.
* ``rejected``     — reproduced but wrong/incomplete (the false completion we
  are hunting).
* ``unverifiable`` — the artifact could not be located or driven (e.g. an
  interactive form we cannot exercise). Honest non-pass, never a false green.
* ``error``        — the runtime needed to reproduce is unavailable (no
  ``node``/``sklearn``) or the harness itself failed. Non-pass.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

PYTHON = os.environ.get("SUITE_PYTHON", sys.executable)
RESULT_SENTINEL = "@@ACCEPTANCE_RESULT@@"

# Directories that are agent-run byproducts, not source. Copying them into the
# independent re-run would let cached state (compiled bytecode, a virtualenv,
# installed node_modules) mask a workspace that is not actually self-contained.
_PRUNE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
               "node_modules", ".venv", "venv", ".tox", ".ruff_cache"}


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    """One authoritative assertion, with the exact command and its captured
    output so a reader can reproduce and audit the verdict by hand."""

    name: str
    passed: bool
    detail: str = ""
    command: list[str] | None = None
    stdout: str = ""
    stderr: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "command": self.command,
            "stdout": _clip(self.stdout),
            "stderr": _clip(self.stderr),
        }


@dataclass
class ContractResult:
    slug: str
    verdict: str = "unverifiable"  # accepted | rejected | unverifiable | error
    reason: str = ""
    target_file: str | None = None
    rerun_dir: str | None = None
    checks: list[Check] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    runner_label: str = "host-unsafe"
    gateable: bool = False           # only a container-run acceptance is gateable

    @property
    def accepted(self) -> bool:
        return self.verdict == "accepted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "verdict": self.verdict,
            "accepted": self.accepted,
            "reason": self.reason,
            "runner_label": self.runner_label,
            "gateable": self.gateable,
            "target_file": self.target_file,
            "rerun_dir": self.rerun_dir,
            "checks": [c.as_dict() for c in self.checks],
            "artifacts": self.artifacts,
        }


def _clip(text: str, limit: int = 4000) -> str:
    if text and len(text) > limit:
        return text[:limit] + f"\n...[{len(text) - limit} bytes truncated]"
    return text or ""


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def isolate(workspace: Path) -> Path:
    """Copy the workspace to a fresh temp dir with agent byproducts stripped.

    This is the "independent re-run outside the agent's own sandbox": the
    returned path is under the system temp root, never inside
    ``~/.forge/workspaces``.
    """

    dest = Path(tempfile.mkdtemp(prefix="forge-accept-"))
    target = dest / "work"
    shutil.copytree(
        workspace,
        target,
        ignore=shutil.ignore_patterns(*_PRUNE_DIRS),
        dirs_exist_ok=True,
    )
    return target


# --------------------------------------------------------------------------- #
# Execution runners — the security boundary for untrusted agent code.
#
# The contracts execute code the agent produced. Doing that with a bare host
# ``subprocess.run`` is unsafe (copying to a temp dir is not a sandbox). Every
# contract command therefore goes through the *active runner*:
#
#   * ContainerRunner  — the only runner that yields a gateable benchmark result:
#     an ephemeral, --network none, non-root, cap-dropped, read-only-root
#     container over a disposable copy of the snapshot.
#   * HostRunner       — developer-only, explicitly labeled UNSAFE and NOT
#     gateable. Used by unit tests to exercise contract *logic*. There is no
#     silent fallback from container to host: choosing host is a deliberate act.
# --------------------------------------------------------------------------- #
class Runner:
    label = "abstract"
    gateable = False

    def run(self, cmd: list[str], cwd: Path, *, timeout: int,
            stdin: str | None = None) -> subprocess.CompletedProcess:
        raise NotImplementedError


class HostRunner(Runner):
    """Runs commands directly on the host. Unsafe; never gateable."""

    label = "host-unsafe"
    gateable = False

    def run(self, cmd, cwd, *, timeout, stdin=None):  # noqa: ANN001
        return subprocess.run(cmd, cwd=str(cwd), input=stdin, text=True,
                              capture_output=True, timeout=timeout)


class ContainerRunner(Runner):
    """Runs each command inside an ephemeral, network-isolated verifier
    container over the isolated snapshot copy. The only gateable runner."""

    label = "container"
    gateable = True

    def __init__(self, image: str | None = None, mount_root: Path | None = None,
                 memory: str = "1g", cpus: str = "1.0", pids: int = 256):
        self.image = image or os.environ.get("FORGE_VERIFIER_IMAGE", "forge-sandbox:latest")
        self.mount_root = Path(mount_root).resolve() if mount_root else None
        self.memory, self.cpus, self.pids = memory, cpus, pids

    def run(self, cmd, cwd, *, timeout, stdin=None):  # noqa: ANN001
        cwd = Path(cwd).resolve()
        root = self.mount_root or cwd
        try:
            rel = cwd.relative_to(root)
        except ValueError:
            root, rel = cwd, Path(".")
        container_cwd = str(PurePosixPath("/verify") / rel)
        translated = [self._to_container(token, root) for token in cmd]
        docker_cmd = [
            "docker", "run", "--rm", "--network", "none",
            "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", str(self.pids),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,exec",
            "--user", "10001:10001",
            "-v", f"{root}:/verify:rw", "-w", container_cwd,
            "-i", self.image, *translated,
        ]
        return subprocess.run(docker_cmd, input=stdin, text=True,
                              capture_output=True, timeout=timeout + 15)

    @staticmethod
    def _to_container(token: str, root: Path) -> str:
        """Rewrite a host command token for the container's filesystem/tools.

        The host interpreter path (this venv's python) does not exist in the
        image, and absolute host paths under the mount must become ``/verify``
        paths. Anything else (flags, code strings, plain args) passes through.
        """
        if token == PYTHON or token == str(PYTHON):
            return "python3"
        try:
            candidate = Path(token)
            if candidate.is_absolute():
                return str(PurePosixPath("/verify") / candidate.resolve().relative_to(root))
        except (ValueError, OSError):
            pass
        return token


# The active runner is context-local so contracts need no signature changes and
# concurrent verifications cannot cross-contaminate.
_ACTIVE_RUNNER: contextvars.ContextVar[Runner] = contextvars.ContextVar(
    "acceptance_active_runner", default=HostRunner())


def active_runner() -> Runner:
    return _ACTIVE_RUNNER.get()


def _run(cmd: list[str], cwd: Path, *, timeout: int, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Execute one contract command through the active runner (never a bare
    host subprocess unless the active runner is explicitly HostRunner)."""
    return active_runner().run(cmd, cwd, timeout=timeout, stdin=stdin)


def discover(root: Path, *, name_hints: tuple[str, ...], content_any: tuple[str, ...],
             suffix: str = ".py") -> Path | None:
    """Find the implementation file by name hint first, then by content.

    Excludes our own injected drivers and obvious test files so we never
    accidentally verify the agent's ``assert True`` against itself.
    """

    candidates = [
        p for p in sorted(root.rglob(f"*{suffix}"))
        if p.is_file()
        and not p.name.startswith("_acceptance")
        and not any(part in _PRUNE_DIRS for part in p.parts)
    ]
    impl = [p for p in candidates if not _looks_like_test(p.name)]

    for hint in name_hints:
        for p in impl:
            if p.name == hint:
                return p
    for hint in name_hints:
        for p in impl:
            if hint in p.name:
                return p
    for p in impl:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(sig in text for sig in content_any):
            return p
    # Last resort: a non-test source file at all, so a single-file solution with
    # an unexpected name is still exercised rather than reported missing.
    return impl[0] if impl else None


def _looks_like_test(name: str) -> bool:
    stem = name.rsplit(".", 1)[0]
    return stem.startswith("test_") or stem.endswith("_test") or stem in {"tests", "conftest"}


def _parse_driver_output(stdout: str) -> dict[str, Any] | None:
    for line in stdout.splitlines():
        if line.startswith(RESULT_SENTINEL):
            try:
                return json.loads(line[len(RESULT_SENTINEL):].strip())
            except json.JSONDecodeError:
                return None
    return None


_PY_PREAMBLE = f"""
import importlib.util, json, sys, traceback

def _load(path):
    spec = importlib.util.spec_from_file_location("agent_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _emit(cases, error=None, extra=None):
    payload = {{"cases": cases, "error": error}}
    if extra:
        payload.update(extra)
    print("{RESULT_SENTINEL} " + json.dumps(payload))

def _find(mod, names):
    for n in names:
        f = getattr(mod, n, None)
        if callable(f):
            return f
    lowered = {{a.lower(): a for a in dir(mod)}}
    for n in names:
        for low, real in lowered.items():
            if n.lower() in low:
                f = getattr(mod, real)
                if callable(f):
                    return f
    return None
"""


def _run_python_driver(root: Path, target: Path, body: str, *, timeout: int = 90) -> tuple[list[str], subprocess.CompletedProcess]:
    driver = root / "_acceptance_driver.py"
    driver.write_text(_PY_PREAMBLE + "\n" + body, encoding="utf-8")
    cmd = [PYTHON, str(driver), str(target)]
    proc = _run(cmd, root, timeout=timeout)
    return cmd, proc


def _cases_to_checks(cmd: list[str], proc: subprocess.CompletedProcess,
                     payload: dict[str, Any] | None) -> tuple[list[Check], str | None]:
    """Translate driver output into checks; return (checks, driver_error)."""
    if payload is None:
        return ([Check("driver produced parseable result", False,
                       detail="no @@ACCEPTANCE_RESULT@@ line in stdout",
                       command=cmd, stdout=proc.stdout, stderr=proc.stderr)], "no result")
    if payload.get("error"):
        return ([Check("driver ran without error", False, detail=str(payload["error"]),
                       command=cmd, stdout=proc.stdout, stderr=proc.stderr)], str(payload["error"]))
    checks: list[Check] = []
    for case in payload.get("cases", []):
        checks.append(Check(
            name=str(case.get("name", "case")),
            passed=bool(case.get("ok")),
            detail=f"got={case.get('got')!r} want={case.get('want')!r}",
        ))
    # Attach the command/output to the first check for auditability.
    if checks:
        checks[0].command = cmd
        checks[0].stdout = proc.stdout
        checks[0].stderr = proc.stderr
    return checks, None


def _finalize(result: ContractResult, checks: list[Check], driver_error: str | None) -> ContractResult:
    result.checks.extend(checks)
    if driver_error:
        result.verdict = "unverifiable"
        result.reason = f"could not exercise artifact: {driver_error}"
    elif not checks:
        result.verdict = "unverifiable"
        result.reason = "no behavioral checks produced"
    elif all(c.passed for c in checks):
        result.verdict = "accepted"
        result.reason = "all authoritative checks passed on independent re-run"
    else:
        failed = [c.name for c in checks if not c.passed]
        result.verdict = "rejected"
        result.reason = "failed checks: " + ", ".join(failed)
    return result


# --------------------------------------------------------------------------- #
# Per-goal contracts
# --------------------------------------------------------------------------- #
def contract_lru(root: Path) -> ContractResult:
    res = ContractResult(slug="lru")
    target = discover(root, name_hints=("lru_cache.py", "lru.py", "cache.py"),
                      content_any=("class LRU", "def put", "def get"))
    if target is None:
        res.reason = "no LRU implementation file found"
        return res
    res.target_file = str(target)
    body = r'''
try:
    mod = _load(sys.argv[1])
    Cls = None
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and hasattr(obj, "get") and hasattr(obj, "put"):
            Cls = obj; break
    if Cls is None:
        _emit([], error="no class exposing both get() and put()"); sys.exit(0)
    try:
        c = Cls(2)
    except TypeError:
        c = Cls(capacity=2)
    def g(k):
        try:
            return c.get(k)
        except KeyError:
            return None
    def miss(v):
        return v in (-1, None)
    cases = []
    c.put(1, 1); c.put(2, 2)
    v = g(1);  cases.append({"name": "get(1) hit", "ok": v == 1, "got": v, "want": 1})
    c.put(3, 3)  # capacity 2 -> evicts LRU key 2
    v = g(2);  cases.append({"name": "get(2) evicted", "ok": miss(v), "got": v, "want": "miss"})
    v = g(3);  cases.append({"name": "get(3) hit", "ok": v == 3, "got": v, "want": 3})
    c.put(4, 4)  # evicts LRU key 1
    v = g(1);  cases.append({"name": "get(1) evicted", "ok": miss(v), "got": v, "want": "miss"})
    v = g(3);  cases.append({"name": "get(3) survives", "ok": v == 3, "got": v, "want": 3})
    v = g(4);  cases.append({"name": "get(4) hit", "ok": v == 4, "got": v, "want": 4})
    src = open(sys.argv[1]).read()
    o1 = "dict" in src or "OrderedDict" in src or "{}" in src
    cases.append({"name": "O(1) map-backed store", "ok": o1, "got": o1, "want": True})
    _emit(cases)
except Exception:
    _emit([], error=traceback.format_exc())
'''
    cmd, proc = _run_python_driver(root, target, body)
    checks, err = _cases_to_checks(cmd, proc, _parse_driver_output(proc.stdout))
    return _finalize(res, checks, err)


_RPN_BATTERY = (("3 4 +", 7), ("5 1 2 + 4 * + 3 -", 14), ("2 3 4 * +", 14),
                ("10 2 /", 5), ("15 7 1 1 + - / 3 *", 9))


def contract_rpn(root: Path) -> ContractResult:
    res = ContractResult(slug="rpn")
    target = discover(root, name_hints=("rpn_calculator.py", "rpn_calc.py", "rpn.py", "calculator.py"),
                      content_any=("def eval", "rpn", "reverse polish", "pop()", "sys.argv"))
    if target is None:
        res.reason = "no RPN implementation file found"
        return res
    res.target_file = str(target)

    # Strategy 1: an importable evaluator function.
    body = r'''
try:
    mod = _load(sys.argv[1])
    f = _find(mod, ["eval_rpn", "evalrpn", "rpn", "evaluate", "calculate", "calc", "compute", "solve"])
    if f is None:
        _emit([], error="no RPN evaluation callable found"); sys.exit(0)
    def call(expr):
        try:
            return f(expr)
        except TypeError:
            return f(expr.split())
    def close(a, b):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    battery = ''' + repr(list(_RPN_BATTERY)) + r'''
    cases = []
    for expr, want in battery:
        try:
            got = call(expr)
            ok = close(got, want)
        except Exception as e:
            got, ok = "raised:" + type(e).__name__, False
        cases.append({"name": "rpn(%r)" % expr, "ok": ok, "got": got, "want": want})
    _emit(cases)
except Exception:
    _emit([], error=traceback.format_exc())
'''
    cmd, proc = _run_python_driver(root, target, body)
    checks, err = _cases_to_checks(cmd, proc, _parse_driver_output(proc.stdout))
    if err is None and checks and all(c.passed for c in checks):
        return _finalize(res, checks, err)

    # Strategy 2: drive it as a CLI with exact commands and captured stdout —
    # the shape of the real workspace calculators (``python rpn.py 3 4 +``).
    cli_checks = _rpn_cli(root, target)
    if cli_checks and all(c.passed for c in cli_checks):
        return _finalize(res, cli_checks, None)

    # Neither strategy verified it: report whichever actually exercised the
    # artifact (rejected) over the one that could not (unverifiable).
    if checks and not err:
        return _finalize(res, checks, err)
    if cli_checks:
        return _finalize(res, cli_checks, None)
    return _finalize(res, checks, err)


def _last_number(text: str) -> float | None:
    found: float | None = None
    for match in re.finditer(r"-?\d+(?:\.\d+)?", text):
        try:
            found = float(match.group())
        except ValueError:
            continue
    return found


def _rpn_cli(root: Path, target: Path) -> list[Check]:
    """Run the calculator once per case as an exact command, parsing the number
    it prints. Calculators disagree on how the expression arrives, so we try the
    common conventions and take whichever passes the whole battery:

    * ``python rpn.py 3 4 +``    — expression as separate argv tokens
    * ``python rpn.py "3 4 +"``  — expression as one quoted argv argument
    * ``echo "3 4 +" | python rpn.py`` — expression on stdin (REPL-style)
    """
    def run_battery(build_cmd, stdin_for) -> list[Check]:
        checks: list[Check] = []
        for expr, want in _RPN_BATTERY:
            cmd = build_cmd(expr)
            stdin = stdin_for(expr)
            try:
                proc = _run(cmd, target.parent, timeout=30, stdin=stdin)
            except subprocess.TimeoutExpired:
                checks.append(Check(f"cli rpn({expr!r})", False, detail="timed out", command=cmd))
                continue
            got = _last_number(proc.stdout)
            ok = got is not None and abs(got - want) < 1e-6
            checks.append(Check(f"cli rpn({expr!r})", ok,
                                detail=f"stdout={proc.stdout.strip()!r} want={want}",
                                command=cmd, stdout=proc.stdout, stderr=proc.stderr))
        return checks

    forms = [
        (lambda e: [PYTHON, target.name, *e.split()], lambda e: None),   # separate tokens
        (lambda e: [PYTHON, target.name, e], lambda e: None),            # one quoted arg
        (lambda e: [PYTHON, target.name], lambda e: e + "\n"),           # stdin
    ]
    best: list[Check] = []
    for build_cmd, stdin_for in forms:
        checks = run_battery(build_cmd, stdin_for)
        if all(c.passed for c in checks):
            return checks
        if sum(c.passed for c in checks) > sum(c.passed for c in best):
            best = checks
    return best


def contract_json_parser(root: Path) -> ContractResult:
    res = ContractResult(slug="json-parser")
    target = discover(root, name_hints=("json_parser.py", "parser.py", "json_parse.py"),
                      content_any=("def parse", "def loads", "tokeniz", "def tokenize"))
    if target is None:
        res.reason = "no JSON parser implementation file found"
        return res
    res.target_file = str(target)

    # Required constraint: implemented from scratch, so the source must not lean
    # on the stdlib json module. This is a genuine behavioral requirement of the
    # goal, checked statically against the agent's own source.
    src = target.read_text(encoding="utf-8", errors="ignore")
    imports_json = bool(re.search(r"^\s*(import\s+json\b|from\s+json\s+import)", src, re.MULTILINE))
    res.checks.append(Check("does not import the json module", not imports_json,
                            detail="found `import json`" if imports_json else "no json import",
                            command=None))

    body = r'''
import json as _stdjson
try:
    mod = _load(sys.argv[1])
    f = _find(mod, ["parse", "loads", "parse_json", "json_parse", "decode", "load"])
    if f is None:
        _emit([], error="no parse callable found"); sys.exit(0)
    battery = [
        '{"a": 1, "b": [1, 2, 3], "c": "hello", "d": true, "e": null, "f": false}',
        '[1, -2, 3.5, "x", true, null, {"k": "v"}]',
        '"just a string"',
        '42',
        'true',
        '{"nested": {"deep": [{"x": [1, 2]}]}}',
    ]
    cases = []
    for text in battery:
        want = _stdjson.loads(text)
        try:
            got = f(text)
            ok = got == want
        except Exception as e:
            got, ok = "raised:" + type(e).__name__, False
        cases.append({"name": "parse(%s)" % (text[:24]), "ok": ok, "got": got, "want": want})
    _emit(cases)
except Exception:
    _emit([], error=traceback.format_exc())
'''
    cmd, proc = _run_python_driver(root, target, body)
    checks, err = _cases_to_checks(cmd, proc, _parse_driver_output(proc.stdout))
    # Fold the static import check in with the behavioral ones.
    static = res.checks[:]
    res.checks = []
    return _finalize(res, static + checks, err)


def contract_dijkstra(root: Path) -> ContractResult:
    res = ContractResult(slug="dijkstra")
    target = discover(root, name_hints=("dijkstra.py", "shortest_path.py", "graph.py"),
                      content_any=("def dijkstra", "shortest", "heapq", "priority"))
    if target is None:
        res.reason = "no Dijkstra implementation file found"
        return res
    res.target_file = str(target)
    body = r'''
import glob, os, importlib.util

EDGES = [("A", "B", 1), ("A", "C", 4), ("B", "C", 2), ("B", "D", 5), ("C", "D", 1)]
GRAPH_DICT = {"A": {"B": 1, "C": 4}, "B": {"C": 2, "D": 5}, "C": {"D": 1}, "D": {}}
WANT = {"B": 1, "C": 3, "D": 4}  # shortest distances from source "A"

def _dist_from(result, node):
    # Normalize the many return shapes into a distance to `node`.
    if isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, dict):
        return result.get(node)
    if isinstance(result, (int, float)):
        return result  # single (start,end) distance
    return None

def _build_graph(mod):
    # Find any class exposing add_edge (in the module or a sibling like graph.py)
    # and populate it with EDGES, so a bespoke Graph type is drivable too.
    classes = []
    def collect(m):
        for a in dir(m):
            o = getattr(m, a)
            if isinstance(o, type) and hasattr(o, "add_edge"):
                classes.append(o)
    collect(mod)
    for p in glob.glob(os.path.join(os.path.dirname(sys.argv[1]), "*.py")):
        if p.endswith("_acceptance_driver.py"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("sib_" + os.path.basename(p)[:-3], p)
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); collect(m)
        except Exception:
            continue
    for Cls in classes:
        try:
            g = Cls()
            for u, v, w in EDGES:
                g.add_edge(u, v, w)
            return g
        except Exception:
            continue
    return None

try:
    mod = _load(sys.argv[1])
    f = _find(mod, ["dijkstra", "shortest_paths", "shortest_path", "shortest"])
    if f is None:
        _emit([], error="no dijkstra callable found"); sys.exit(0)
    G = _build_graph(mod)
    # Candidate ways to obtain the distance to a target node, most specific last.
    def getters(node):
        yield lambda: _dist_from(f(GRAPH_DICT, "A"), node)
        yield lambda: _dist_from(f("A", GRAPH_DICT), node)
        if G is not None:
            yield lambda: _dist_from(f(G, "A", node), node)
            yield lambda: _dist_from(f(G, "A"), node)
    # Pick the first calling convention that yields a finite distance to "D".
    winner_index = None
    for i, getter in enumerate(getters("D")):
        try:
            d = getter()
            if isinstance(d, (int, float)) and d != float("inf"):
                winner_index = i; break
        except Exception:
            continue
    if winner_index is None:
        _emit([], error="dijkstra callable produced no finite distance for any known signature"); sys.exit(0)
    cases = []
    for node, w in WANT.items():
        try:
            g = list(getters(node))[winner_index]()
            ok = abs(float(g) - w) < 1e-6
        except Exception as e:
            g, ok = "raised:" + type(e).__name__, False
        cases.append({"name": "dist(A->%s)" % node, "ok": ok, "got": g, "want": w})
    _emit(cases)
except Exception:
    _emit([], error=traceback.format_exc())
'''
    cmd, proc = _run_python_driver(root, target, body)
    checks, err = _cases_to_checks(cmd, proc, _parse_driver_output(proc.stdout))
    return _finalize(res, checks, err)


def contract_tic_tac_toe(root: Path) -> ContractResult:
    res = ContractResult(slug="tic-tac-toe")
    target = discover(root, name_hints=("tic_tac_toe.py", "tictactoe.py", "game.py"),
                      content_any=("winner", "check_win", "def is_winner", "tic"))
    if target is None:
        res.reason = "no tic-tac-toe implementation file found"
        return res
    res.target_file = str(target)
    body = r'''
try:
    mod = _load(sys.argv[1])
    f = _find(mod, ["check_winner", "get_winner", "determine_winner", "winner",
                    "check_win", "is_winner", "who_won", "check_victory"])
    if f is None:
        _emit([], error="no winner-detection callable found"); sys.exit(0)
    def as_x(v):
        return v in ("X", "x", True, 1) or v == "X"
    def as_none(v):
        return v in (None, "", " ", 0, False, "draw", "Draw", "DRAW", "tie", "Tie", "none", "None")
    x_row_flat = ["X", "X", "X", "O", "O", " ", " ", " ", " "]
    x_row_grid = [["X", "X", "X"], ["O", "O", " "], [" ", " ", " "]]
    o_col_grid = [["O", "X", "X"], ["O", "X", " "], ["O", " ", " "]]
    draw_grid  = [["X", "O", "X"], ["X", "O", "O"], ["O", "X", "X"]]
    def call(board):
        try:
            return ("ok", f(board))
        except Exception as e:
            return ("err", type(e).__name__)
    cases = []
    # X wins on the top row (try grid then flat representation).
    tag, xw = call(x_row_grid)
    if tag == "err":
        tag, xw = call(x_row_flat)
    cases.append({"name": "detects X row win", "ok": tag == "ok" and as_x(xw), "got": xw, "want": "X"})
    # O wins down the left column.
    tag, ow = call(o_col_grid)
    cases.append({"name": "detects O column win", "ok": tag == "ok" and (ow in ("O", "o", True) or ow == "O"), "got": ow, "want": "O"})
    # Full board, no line: must not report a winner.
    tag, dr = call(draw_grid)
    cases.append({"name": "no winner on drawn board", "ok": tag == "ok" and as_none(dr), "got": dr, "want": "no winner"})
    _emit(cases)
except Exception:
    _emit([], error=traceback.format_exc())
'''
    cmd, proc = _run_python_driver(root, target, body)
    checks, err = _cases_to_checks(cmd, proc, _parse_driver_output(proc.stdout))
    return _finalize(res, checks, err)


def contract_digits(root: Path) -> ContractResult:
    """Re-run the training command and require a reproduced results.json whose
    accuracy is a real number above 0.95 — not merely a present file."""
    res = ContractResult(slug="digits")
    target = discover(root, name_hints=("main.py", "digits.py", "experiment.py", "train.py"),
                      content_any=("load_digits", "sklearn", "accuracy"))
    if target is None:
        res.reason = "no digits experiment file found"
        return res
    res.target_file = str(target)

    # Remove any results.json the agent left behind so we only ever judge the
    # artifact our own independent re-run reproduces.
    for stale in root.rglob("results.json"):
        try:
            stale.unlink()
        except OSError:
            pass

    cmd = [PYTHON, target.name]
    try:
        proc = _run(cmd, target.parent, timeout=600)
    except subprocess.TimeoutExpired as exc:
        res.verdict = "rejected"
        res.reason = "training command did not finish within 600s"
        res.checks.append(Check("training command completes", False, detail=str(exc), command=cmd))
        return res

    ran_ok = proc.returncode == 0
    res.checks.append(Check("training command exits 0", ran_ok,
                            detail=f"returncode={proc.returncode}", command=cmd,
                            stdout=proc.stdout, stderr=proc.stderr))
    if not ran_ok:
        # Distinguish a missing runtime (environment error) from a real crash.
        if "ModuleNotFoundError" in proc.stderr or "ImportError" in proc.stderr:
            res.verdict = "error"
            res.reason = "training dependency unavailable in re-run environment"
        else:
            res.verdict = "rejected"
            res.reason = "training command failed on independent re-run"
        return res

    produced = sorted(target.parent.rglob("results.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not produced:
        res.verdict = "rejected"
        res.reason = "no results.json produced by the training run"
        res.checks.append(Check("results.json is produced", False,
                                detail="training ran but wrote no results.json"))
        return res

    results_path = produced[0]
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        res.verdict = "rejected"
        res.reason = f"results.json is not valid JSON: {exc}"
        res.checks.append(Check("results.json is valid JSON", False, detail=str(exc)))
        return res

    accuracy = _extract_accuracy(data)
    res.artifacts["results.json"] = data
    res.artifacts["accuracy"] = accuracy
    if accuracy is None:
        res.verdict = "rejected"
        res.reason = "results.json contains no numeric accuracy field"
        res.checks.append(Check("results.json reports accuracy", False,
                                detail=f"keys={sorted(data) if isinstance(data, dict) else type(data).__name__}"))
        return res

    above = accuracy > 0.95
    res.checks.append(Check("accuracy > 0.95", above, detail=f"accuracy={accuracy}"))
    res.verdict = "accepted" if above else "rejected"
    res.reason = (f"reproduced accuracy {accuracy:.4f} > 0.95" if above
                  else f"reproduced accuracy {accuracy} is not above 0.95")
    return res


def _extract_accuracy(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    for key in ("accuracy", "acc", "test_accuracy", "accuracy_score", "score", "val_accuracy"):
        if key in data:
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                continue
            # Tolerate a 0-100 percentage encoding.
            return value / 100.0 if value > 1.0 else value
    return None


def contract_email_validator(root: Path) -> ContractResult:
    """Drive the JS validator under node with a valid/invalid battery."""
    res = ContractResult(slug="email-validator")
    node = shutil.which("node") or (os.environ.get("SUITE_NODE"))
    target = discover(root, name_hints=("email_validator.js", "validator.js", "email.js", "index.js"),
                      content_any=("email", "@", "regex", "test("), suffix=".js")
    if target is None:
        res.reason = "no JavaScript validator file found"
        return res
    res.target_file = str(target)
    if not node:
        res.verdict = "error"
        res.reason = "node runtime unavailable to re-run the JS validator"
        return res

    driver = root / "_acceptance_driver.js"
    driver.write_text(_JS_DRIVER, encoding="utf-8")
    cmd = [node, str(driver), str(target)]
    try:
        proc = _run(cmd, root, timeout=60)
    except subprocess.TimeoutExpired as exc:
        res.verdict = "unverifiable"
        res.reason = f"validator did not respond within timeout: {exc}"
        return res

    payload = _parse_driver_output(proc.stdout)
    checks, err = _cases_to_checks(cmd, proc, payload)
    return _finalize(res, checks, err)


_JS_DRIVER = r'''
const path = require("path");
function emit(cases, error) {
  console.log("''' + RESULT_SENTINEL + r''' " + JSON.stringify({cases: cases, error: error || null}));
}
try {
  let mod = require(path.resolve(process.argv[2]));
  let fn = null;
  if (typeof mod === "function") fn = mod;
  else if (mod && typeof mod.default === "function") fn = mod.default;
  else if (mod) {
    for (const k of ["validate", "validateEmail", "isValid", "isValidEmail", "isEmail", "emailValidator", "checkEmail", "validEmail"]) {
      if (typeof mod[k] === "function") { fn = mod[k]; break; }
    }
  }
  if (!fn) { emit([], "no validator function exported (module.exports)"); process.exit(0); }
  const valid = ["john@example.com", "john.doe@example.co.uk", "a_b+c@sub.domain.org"];
  const invalid = ["plainaddress", "@no-local.com", "no-at.com", "a@b", "a b@c.com", "two@@at.com", ""];
  const cases = [];
  for (const e of valid) {
    let ok = false, got;
    try { got = fn(e); ok = !!got; } catch (err) { got = "threw:" + err.name; }
    cases.push({name: "valid: " + JSON.stringify(e), ok: ok, got: got, want: true});
  }
  for (const e of invalid) {
    let ok = false, got;
    try { got = fn(e); ok = !got; } catch (err) { got = "threw:" + err.name; ok = false; }
    cases.push({name: "invalid: " + JSON.stringify(e), ok: ok, got: got, want: false});
  }
  emit(cases);
} catch (err) {
  emit([], "driver failed to load module: " + (err && err.message ? err.message : String(err)));
}
'''


def contract_snake(root: Path) -> ContractResult:
    """Snake is an interactive curses program, so we cannot assert a score. We
    can still separate a real game loop from a stub: run it under a fake curses
    that scripts key presses and counts draws, then require evidence of an
    input-driven render loop. The ``print("Snake initialized")`` placeholder
    makes zero getch calls and is rejected."""
    res = ContractResult(slug="snake")
    target = discover(root, name_hints=("main.py", "snake.py", "game.py"),
                      content_any=("import curses", "curses.wrapper", "curses"))
    if target is None:
        res.reason = "no Snake implementation file found"
        return res
    res.target_file = str(target)
    src = target.read_text(encoding="utf-8", errors="ignore")
    if "curses" not in src:
        res.verdict = "rejected"
        res.reason = "goal requires curses; implementation does not use it"
        res.checks.append(Check("uses curses", False, detail="no curses import"))
        return res

    # A stub curses that shadows the stdlib one (cwd is sys.path[0] for a script
    # run), feeds scripted moves, counts render calls, and exits the loop after
    # a fixed budget so an otherwise-infinite game terminates deterministically.
    (root / "curses.py").write_text(_CURSES_STUB, encoding="utf-8")
    probe = root / "_snake_probe.json"
    if probe.exists():
        probe.unlink()

    cmd = [PYTHON, target.name]
    try:
        proc = _run(cmd, target.parent, timeout=45)
    except subprocess.TimeoutExpired as exc:
        res.verdict = "rejected"
        res.reason = "game loop never terminated even under a scripted-input stub"
        res.checks.append(Check("terminates under scripted input", False, detail=str(exc), command=cmd))
        return res

    probe_path = target.parent / "_snake_probe.json"
    if not probe_path.exists():
        res.verdict = "unverifiable"
        res.reason = "program did not run under the curses stub (no probe written)"
        res.checks.append(Check("runs under curses stub", False, command=cmd,
                                stdout=proc.stdout, stderr=proc.stderr))
        return res

    stats = json.loads(probe_path.read_text(encoding="utf-8"))
    res.artifacts["probe"] = stats
    getch = stats.get("getch_calls", 0)
    draws = stats.get("draw_calls", 0)
    crashed = stats.get("crashed")

    loop = Check("input-driven render loop (>=3 getch and >=3 draws)",
                 getch >= 3 and draws >= 3,
                 detail=f"getch_calls={getch} draw_calls={draws}", command=cmd,
                 stdout=proc.stdout, stderr=proc.stderr)
    no_crash = Check("runs without crashing under scripted input", not crashed,
                     detail=(crashed or "clean"))
    res.checks.extend([loop, no_crash])
    if loop.passed and no_crash.passed:
        res.verdict = "accepted"
        res.reason = f"drove {getch} frames of an input-driven curses loop"
    elif not loop.passed:
        res.verdict = "rejected"
        res.reason = "no input-driven game loop (looks like a placeholder)"
    else:
        res.verdict = "rejected"
        res.reason = f"crashed under scripted input: {crashed}"
    return res


_CURSES_STUB = r'''
"""Minimal curses stand-in for headless acceptance of a Snake game.

Shadows the stdlib curses module (the workspace dir is sys.path[0] when the
game is run as a script). Scripts a fixed key sequence, counts render calls,
and forces the loop to end after a budget so we can observe whether the program
is an input-driven render loop rather than a one-shot stub.
"""
import atexit, json, os

_MOVES = [259, 261, 258, 260] * 40  # UP, RIGHT, DOWN, LEFT arrow codes, repeated
_state = {"getch_calls": 0, "draw_calls": 0, "crashed": None}
_BUDGET = 60

KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 259, 258, 260, 261
KEY_ENTER = 10
A_BOLD = A_NORMAL = A_REVERSE = A_BLINK = A_DIM = A_UNDERLINE = 0
COLOR_BLACK = COLOR_RED = COLOR_GREEN = COLOR_YELLOW = 0
COLOR_BLUE = COLOR_MAGENTA = COLOR_CYAN = COLOR_WHITE = 0


class error(Exception):
    pass


def _dump():
    try:
        with open(os.path.join(os.getcwd(), "_snake_probe.json"), "w") as f:
            json.dump(_state, f)
    except OSError:
        pass


atexit.register(_dump)


class _Win:
    def getch(self, *a, **k):
        i = _state["getch_calls"]
        _state["getch_calls"] += 1
        if i >= _BUDGET:
            raise SystemExit(0)  # end an otherwise-infinite loop deterministically
        return _MOVES[i % len(_MOVES)]

    def getkey(self, *a, **k):
        return chr(self.getch() % 128)

    def addstr(self, *a, **k):
        _state["draw_calls"] += 1

    def addch(self, *a, **k):
        _state["draw_calls"] += 1

    def refresh(self, *a, **k):
        _state["draw_calls"] += 1

    def noutrefresh(self, *a, **k):
        _state["draw_calls"] += 1

    def getmaxyx(self):
        return (24, 80)

    def __getattr__(self, name):
        def _noop(*a, **k):
            return 0
        return _noop


_screen = _Win()


def initscr():
    return _screen


def wrapper(func, *args, **kwargs):
    try:
        return func(_screen, *args, **kwargs)
    except SystemExit:
        raise
    except BaseException as exc:  # record so the contract can report a crash
        _state["crashed"] = type(exc).__name__ + ": " + str(exc)[:200]
        return None


def newwin(*a, **k):
    return _Win()


def color_pair(n):
    return 0


def napms(*a, **k):
    pass


def __getattr__(name):  # module-level fallback for any other curses.* symbol
    def _noop(*a, **k):
        return 0
    return _noop
'''


# --------------------------------------------------------------------------- #
# Manifest-goal contracts (evals/suite/manifest.json, consumed by runner.py)
#
# These are the "known-good contract per goal" the manifest promises. Same
# discipline as the PGE contracts above: isolate the workspace, drive it with
# exact commands, decide from real behavior — never a bare exit 0.
# --------------------------------------------------------------------------- #
def contract_fizzbuzz(root: Path) -> ContractResult:
    res = ContractResult(slug="python-fizzbuzz")
    target = discover(root, name_hints=("fizzbuzz.py", "fizz_buzz.py"),
                      content_any=("def fizzbuzz", "FizzBuzz", "Fizz"))
    if target is None:
        res.reason = "no fizzbuzz.py found"
        return res
    res.target_file = str(target)
    body = r'''
try:
    mod = _load(sys.argv[1])
    f = _find(mod, ["fizzbuzz", "fizz_buzz", "fb"])
    if f is None:
        _emit([], error="no fizzbuzz callable found"); sys.exit(0)
    def want(n):
        if n % 15 == 0: return "FizzBuzz"
        if n % 3 == 0: return "Fizz"
        if n % 5 == 0: return "Buzz"
        return n
    cases = []
    for n in range(1, 16):
        w = want(n)
        try:
            got = f(n)
            if isinstance(w, str):
                ok = str(got) == w
            else:
                ok = got == n or str(got) == str(n)
        except Exception as e:
            got, ok = "raised:" + type(e).__name__, False
        cases.append({"name": "fizzbuzz(%d)" % n, "ok": ok, "got": got, "want": w})
    _emit(cases)
except Exception:
    _emit([], error=traceback.format_exc())
'''
    cmd, proc = _run_python_driver(root, target, body)
    checks, err = _cases_to_checks(cmd, proc, _parse_driver_output(proc.stdout))
    # Honor the manifest's "pytest passes" criterion if the agent shipped a suite.
    test_files = [p for p in root.rglob("test_*.py")] + [p for p in root.rglob("*_test.py")]
    if test_files and shutil.which(PYTHON) is not None:
        tcmd = [PYTHON, "-m", "pytest", "-q"]
        try:
            tproc = _run(tcmd, root, timeout=120)
            checks.append(Check("agent pytest suite passes", tproc.returncode == 0,
                                detail=f"returncode={tproc.returncode}", command=tcmd,
                                stdout=tproc.stdout, stderr=tproc.stderr))
        except subprocess.TimeoutExpired as exc:
            checks.append(Check("agent pytest suite passes", False, detail=str(exc), command=tcmd))
    return _finalize(res, checks, err)


def contract_node_cli(root: Path) -> ContractResult:
    res = ContractResult(slug="node-cli-arg-parser")
    node = shutil.which("node") or os.environ.get("SUITE_NODE")
    target = discover(root, name_hints=("parser.js", "args.js", "cli.js", "argparse.js", "index.js"),
                      content_any=("--name", "--count", "process.argv", "argv"), suffix=".js")
    if target is None:
        res.reason = "no JS parser file found"
        return res
    res.target_file = str(target)
    if not node:
        res.verdict = "error"
        res.reason = "node runtime unavailable"
        return res

    checks: list[Check] = []
    # Authoritative behavior: parse --name alice --count 3 -> {name: alice, count: 3}.
    driver = root / "_acceptance_node_cli.js"
    driver.write_text(_NODE_CLI_DRIVER, encoding="utf-8")
    dcmd = [node, str(driver), str(target)]
    got_behavior = False
    try:
        dproc = _run(dcmd, root, timeout=60)
        payload = _parse_driver_output(dproc.stdout)
        if payload and not payload.get("error"):
            bchecks, _ = _cases_to_checks(dcmd, dproc, payload)
            checks.extend(bchecks)
            got_behavior = True
    except subprocess.TimeoutExpired:
        pass
    if not got_behavior:
        # Fall back to driving it as a CLI: node parser.js --name alice --count 3.
        ccmd = [node, target.name, "--name", "alice", "--count", "3"]
        try:
            cproc = _run(ccmd, target.parent, timeout=60)
            out = cproc.stdout.lower()
            ok = "alice" in out and "3" in cproc.stdout
            checks.append(Check("cli parses --name/--count", ok,
                                detail=f"stdout={cproc.stdout.strip()!r}", command=ccmd,
                                stdout=cproc.stdout, stderr=cproc.stderr))
        except subprocess.TimeoutExpired as exc:
            checks.append(Check("cli parses --name/--count", False, detail=str(exc), command=ccmd))

    # Honor "node --test passes" if the agent shipped tests.
    if any(root.rglob("*.test.js")) or any("node:test" in p.read_text(errors="ignore")
                                           for p in root.rglob("*.js") if p.name != driver.name):
        tcmd = [node, "--test"]
        try:
            tproc = _run(tcmd, root, timeout=90)
            checks.append(Check("node --test passes", tproc.returncode == 0,
                                detail=f"returncode={tproc.returncode}", command=tcmd,
                                stdout=tproc.stdout, stderr=tproc.stderr))
        except subprocess.TimeoutExpired as exc:
            checks.append(Check("node --test passes", False, detail=str(exc), command=tcmd))
    return _finalize(res, checks, None)


_NODE_CLI_DRIVER = r'''
const path = require("path");
function emit(cases, error) {
  console.log("''' + RESULT_SENTINEL + r''' " + JSON.stringify({cases: cases, error: error || null}));
}
try {
  let mod = require(path.resolve(process.argv[2]));
  let fn = null;
  if (typeof mod === "function") fn = mod;
  else if (mod && typeof mod.default === "function") fn = mod.default;
  else if (mod) {
    for (const k of ["parse", "parseArgs", "parseArguments", "parser", "argparse"]) {
      if (typeof mod[k] === "function") { fn = mod[k]; break; }
    }
  }
  if (!fn) { emit([], "no parser function exported"); process.exit(0); }
  const argv = ["--name", "alice", "--count", "3"];
  let out;
  try { out = fn(argv); } catch (e) { emit([], "parser threw: " + e.name); process.exit(0); }
  const name = out && (out.name !== undefined ? out.name : out.Name);
  const count = out && (out.count !== undefined ? out.count : out.Count);
  const cases = [
    {name: "parses --name", ok: name === "alice", got: name, want: "alice"},
    {name: "parses --count", ok: String(count) === "3", got: count, want: 3},
  ];
  emit(cases);
} catch (err) {
  emit([], "driver failed to load module: " + (err && err.message ? err.message : String(err)));
}
'''


def contract_cpp_reverse(root: Path) -> ContractResult:
    res = ContractResult(slug="cpp-string-reverse")
    cxx = shutil.which("g++") or shutil.which("clang++") or os.environ.get("SUITE_CXX")
    impl = None
    for p in sorted(root.rglob("*.cpp")):
        if "_acceptance" in p.name:
            continue
        text = p.read_text(errors="ignore")
        if "reverse_string" in text:
            impl = p
            break
    if impl is None:
        res.reason = "no .cpp defining reverse_string found"
        return res
    res.target_file = str(impl)
    if not cxx:
        res.verdict = "error"
        res.reason = "no C++ compiler available"
        return res

    # Compile an authoritative harness that calls the agent's reverse_string and
    # asserts round-trip correctness. We try the two common signatures; if the
    # agent's function has our main too, exclude their main to avoid a clash.
    others = [p for p in root.rglob("*.cpp") if p != impl and "_acceptance" not in p.name]
    sources = [str(impl)] + [str(p) for p in others if "int main" not in p.read_text(errors="ignore")]
    harness = root / "_acceptance_cpp_main.cpp"
    for sig in ('std::string reverse_string(const std::string&);',
                'std::string reverse_string(std::string);'):
        harness.write_text(_CPP_HARNESS.replace("__SIG__", sig), encoding="utf-8")
        binpath = root / "_acceptance_cpp_bin"
        ccmd = [cxx, "-std=c++17", "-o", str(binpath), str(harness), *[s for s in sources if "int main" not in Path(s).read_text(errors="ignore")]]
        try:
            build = _run(ccmd, root, timeout=120)
        except subprocess.TimeoutExpired as exc:
            res.checks.append(Check("compile authoritative harness", False, detail=str(exc), command=ccmd))
            continue
        if build.returncode != 0:
            continue  # try the next signature
        try:
            runp = _run([str(binpath)], root, timeout=30)
        except subprocess.TimeoutExpired as exc:
            res.checks.append(Check("run authoritative harness", False, detail=str(exc)))
            continue
        ok = runp.returncode == 0 and "ALL_OK" in runp.stdout
        res.checks.append(Check("reverse_string round-trips (independent harness)", ok,
                                detail=f"returncode={runp.returncode} stdout={runp.stdout.strip()!r}",
                                command=[str(binpath)], stdout=runp.stdout, stderr=runp.stderr))
        return _finalize(res, res.checks, None)
    res.verdict = "unverifiable"
    res.reason = "could not compile a harness against reverse_string (unexpected signature)"
    res.checks.append(Check("compile authoritative harness", False,
                            detail="neither const-ref nor by-value signature linked"))
    return res


_CPP_HARNESS = r'''
#include <string>
#include <iostream>
__SIG__
int main() {
    if (reverse_string(std::string("abc")) != "cba") { std::cout << "FAIL_abc\n"; return 1; }
    if (reverse_string(std::string("")) != "") { std::cout << "FAIL_empty\n"; return 1; }
    if (reverse_string(reverse_string(std::string("hello"))) != "hello") { std::cout << "FAIL_roundtrip\n"; return 1; }
    std::cout << "ALL_OK\n";
    return 0;
}
'''


def contract_rust_word_count(root: Path) -> ContractResult:
    res = ContractResult(slug="rust-word-count")
    cargo = shutil.which("cargo") or os.environ.get("SUITE_CARGO")
    manifests = [p for p in root.rglob("Cargo.toml")]
    if not manifests:
        res.reason = "no Cargo.toml found"
        return res
    crate = min(manifests, key=lambda p: len(p.parts)).parent
    res.target_file = str(crate / "Cargo.toml")
    if not cargo:
        res.verdict = "error"
        res.reason = "cargo unavailable"
        return res

    checks: list[Check] = []
    # cargo test is the manifest's stated criterion — run it, capture output.
    tcmd = [cargo, "test", "--quiet"]
    try:
        tproc = _run(tcmd, crate, timeout=600)
        checks.append(Check("cargo test passes", tproc.returncode == 0,
                            detail=f"returncode={tproc.returncode}", command=tcmd,
                            stdout=tproc.stdout, stderr=tproc.stderr))
    except subprocess.TimeoutExpired as exc:
        checks.append(Check("cargo test passes", False, detail=str(exc), command=tcmd))
        return _finalize(res, checks, None)

    # Authoritative behavior: build the bin and count words from stdin.
    bcmd = [cargo, "build", "--quiet"]
    try:
        bproc = _run(bcmd, crate, timeout=600)
    except subprocess.TimeoutExpired as exc:
        checks.append(Check("cargo build succeeds", False, detail=str(exc), command=bcmd))
        return _finalize(res, checks, None)
    if bproc.returncode == 0:
        rcmd = [cargo, "run", "--quiet"]
        try:
            rproc = _run(rcmd, crate, timeout=120, stdin="the quick brown fox\njumps over\n")
            got = _last_number(rproc.stdout)
            ok = got is not None and int(got) == 6
            checks.append(Check("counts words from stdin (== 6)", ok,
                                detail=f"stdout={rproc.stdout.strip()!r}", command=rcmd,
                                stdout=rproc.stdout, stderr=rproc.stderr))
        except subprocess.TimeoutExpired as exc:
            checks.append(Check("counts words from stdin", False, detail=str(exc), command=rcmd))
    else:
        checks.append(Check("cargo build succeeds", False,
                            detail=f"returncode={bproc.returncode}", command=bcmd,
                            stdout=bproc.stdout, stderr=bproc.stderr))
    return _finalize(res, checks, None)


def contract_nonvacuous_artifact(root: Path) -> ContractResult:
    """Best-effort contract for the adversarial python goals whose whole point is
    that a naive acceptance test would be vacuous. We require an independently
    checkable artifact: a runnable script that emits observable output or writes
    a file, plus a test file that actually asserts something non-trivial. This
    can never green a workspace whose only "proof" is ``python -c 'import os'``."""
    res = ContractResult(slug="nonvacuous-artifact")
    sources = [p for p in root.rglob("*.py")
               if not p.name.startswith("_acceptance") and not _looks_like_test(p.name)]
    if not sources:
        res.reason = "no python source artifact found"
        return res

    checks: list[Check] = []
    # 1) Some script produces observable output or a file when executed.
    observable = False
    for src in sources:
        before = {p for p in root.rglob("*") if p.is_file()}
        cmd = [PYTHON, src.name]
        try:
            proc = _run(cmd, src.parent, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        after = {p for p in root.rglob("*") if p.is_file()}
        wrote_file = bool(after - before)
        if proc.returncode == 0 and (proc.stdout.strip() or wrote_file):
            observable = True
            checks.append(Check("produces an observable artifact", True,
                                detail=f"{src.name}: stdout={proc.stdout.strip()[:60]!r} new_files={len(after - before)}",
                                command=cmd, stdout=proc.stdout, stderr=proc.stderr))
            break
    if not observable:
        checks.append(Check("produces an observable artifact", False,
                            detail="no script emitted output or wrote a file"))

    # 2) A test file exists with a non-vacuous assertion.
    non_vacuous = False
    for tf in list(root.rglob("test_*.py")) + list(root.rglob("*_test.py")):
        text = tf.read_text(errors="ignore")
        asserts = re.findall(r"assert\s+(.+)", text)
        meaningful = [a for a in asserts if a.strip() not in ("True", "1", "1 == 1", "True is True")]
        if meaningful:
            non_vacuous = True
            break
    checks.append(Check("has a non-vacuous test assertion", non_vacuous,
                        detail="found a real assert" if non_vacuous else "only vacuous or missing asserts"))
    return _finalize(res, checks, None)


MANIFEST_CONTRACTS: dict[str, Callable[[Path], ContractResult]] = {
    "python-fizzbuzz": contract_fizzbuzz,
    "python-vacuous-contract-trap": contract_nonvacuous_artifact,
    "python-flaky-test": contract_nonvacuous_artifact,
    "node-cli-arg-parser": contract_node_cli,
    "cpp-string-reverse": contract_cpp_reverse,
    "rust-word-count": contract_rust_word_count,
}


def verify_manifest_goal(goal_id: str, workspace: str | os.PathLike[str] | None, *,
                         keep_rerun: bool = False, runner: Runner | None = None) -> dict[str, Any]:
    """Acceptance contract for a manifest goal id (evals/suite/manifest.json)."""
    return _verify_with(MANIFEST_CONTRACTS, goal_id, workspace, keep_rerun=keep_rerun, runner=runner)


# --------------------------------------------------------------------------- #
# Registry + entry point
# --------------------------------------------------------------------------- #
CONTRACTS: dict[str, Callable[[Path], ContractResult]] = {
    "snake": contract_snake,
    "digits": contract_digits,
    "lru": contract_lru,
    "rpn": contract_rpn,
    "json-parser": contract_json_parser,
    "tic-tac-toe": contract_tic_tac_toe,
    "dijkstra": contract_dijkstra,
    "email-validator": contract_email_validator,
}


def _verify_with(registry: dict[str, Callable[[Path], ContractResult]], key: str,
                 workspace: str | os.PathLike[str] | None, *,
                 keep_rerun: bool = False, runner: Runner | None = None) -> dict[str, Any]:
    """Run ``registry[key]`` against an isolated copy of ``workspace``.

    Always returns a JSON-serializable dict; never raises. A missing workspace
    or unknown key yields an honest non-``accepted`` verdict rather than a
    fabricated pass. Every contract command runs through ``runner`` (default:
    the host runner, whose results are marked non-gateable); the orchestrator
    passes a ``ContainerRunner`` to produce a gateable benchmark verdict.
    """

    if key not in registry:
        return ContractResult(slug=key, verdict="unverifiable",
                              reason=f"no acceptance contract defined for {key!r}").as_dict()
    if not workspace:
        return ContractResult(slug=key, verdict="unverifiable",
                              reason="no workspace path provided").as_dict()
    ws = Path(workspace)
    if not ws.exists():
        return ContractResult(slug=key, verdict="unverifiable",
                              reason=f"workspace path does not exist: {ws}").as_dict()

    rerun = isolate(ws)
    active = runner or HostRunner()
    if isinstance(active, ContainerRunner) and active.mount_root is None:
        # The container must mount the exact isolated copy the contract reads.
        active = ContainerRunner(image=active.image, mount_root=rerun,
                                 memory=active.memory, cpus=active.cpus, pids=active.pids)
    token = _ACTIVE_RUNNER.set(active)
    try:
        result = registry[key](rerun)
        result.rerun_dir = str(rerun)
        result.runner_label = active.label
        result.gateable = active.gateable
        return result.as_dict()
    except Exception as exc:  # a contract bug must not crash the whole suite
        return ContractResult(slug=key, verdict="error", runner_label=active.label,
                              gateable=active.gateable,
                              reason=f"contract raised: {type(exc).__name__}: {exc}",
                              rerun_dir=str(rerun)).as_dict()
    finally:
        _ACTIVE_RUNNER.reset(token)
        if not keep_rerun:
            shutil.rmtree(rerun.parent, ignore_errors=True)


def verify_goal(slug: str, workspace: str | os.PathLike[str] | None, *,
                keep_rerun: bool = False, runner: Runner | None = None) -> dict[str, Any]:
    """Run the PGE-suite acceptance contract for ``slug`` against ``workspace``."""
    return _verify_with(CONTRACTS, slug, workspace, keep_rerun=keep_rerun, runner=runner)


def contract_hash() -> str:
    """Content hash of the contract registry + injected drivers.

    Recorded in every result's environment fingerprint so the gate can refuse to
    compare runs judged by different acceptance logic. Changing a contract or a
    driver string changes this hash, which is exactly what should make an old
    baseline incomparable.
    """
    import hashlib

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
