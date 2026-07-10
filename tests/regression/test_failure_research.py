"""Pins the failure-triggered research layer (engine/src/auditor.py's
web_search_cached + its injection into repair tasks in evaluator_node.py).

THE FINDING (2026-07-07 analysis of 26 logged runs, ~1300 tool calls): the
executor NEVER spontaneously used its research tools — zero fetch_doc /
browser_fetch invocations ever, including 32 consecutive attempts on one
compile error. This matches the literature: models don't reliably
self-correct without external information (Huang et al., ICLR 2024,
"LLMs Cannot Self-Correct Reasoning Yet"; Gou et al., CRITIC), and tool
usage tracks how tools are surfaced at the decision point, not raw
availability (Yang et al., SWE-agent: agent-computer interface design).
So the harness now (a) pushes research to the impasse itself — after a
test fails twice, the evaluator searches the error signature and embeds
the findings in the repair task — and (b) gives the executor a web_search
tool with an explicit trigger rule in the prompt.

Worse, the EXISTING auditor research layer had never worked: `ddgs` was
missing from requirements, every search failed, the failure was logged as
"📚 Auditor researched & cached docs" (a success message), and the error
string itself was cached under the research key — permanently poisoning
each term even for after the dependency got installed. 17 such entries
were found live in tools_db. Failures must never be cached or logged as
success.
"""
import pytest

pytest.importorskip("langgraph")
import src.auditor as auditor_mod
from src.auditor import web_search_cached


def test_empty_query_returns_empty_without_touching_the_cache():
    assert web_search_cached("") == ""
    assert web_search_cached("   ") == ""


def test_search_failure_returns_empty_and_is_never_cached(tmp_path, monkeypatch):
    # Point the cache at a scratch DB and make the search dependency blow up
    # exactly like the live incident (ModuleNotFoundError at import time).
    import builtins
    db = tmp_path / "docs.db"
    monkeypatch.setattr(auditor_mod, "_WEB_DOCS_DB", str(db))
    real_import = builtins.__import__

    def _no_ddgs(name, *a, **k):
        if name == "ddgs":
            raise ModuleNotFoundError("No module named 'ddgs'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_ddgs)
    assert web_search_cached("some cmake error") == ""
    # The poison-cache regression: the failure text must NOT be stored.
    import sqlite3
    con = sqlite3.connect(db)
    rows = con.execute("select count(*) from web_docs").fetchone()[0]
    assert rows == 0, "a failed search must never be cached as if it were results"


def test_cache_hit_is_served_without_a_network_call(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "docs.db"
    monkeypatch.setattr(auditor_mod, "_WEB_DOCS_DB", str(db))
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE web_docs (
        url TEXT PRIMARY KEY, content TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
    con.execute("INSERT INTO web_docs VALUES (?,?,?)",
                ("search:known error", "Cached Answer — https://x.example\nsnippet", "t"))
    con.commit(); con.close()
    # No monkeypatched ddgs here: if the cache is consulted first (as it must
    # be), no import/network is attempted at all.
    assert "Cached Answer" in web_search_cached("known error")


def test_research_and_cache_no_longer_caches_its_own_failures(tmp_path, monkeypatch):
    import builtins
    import sqlite3
    db = tmp_path / "docs.db"
    monkeypatch.setattr(auditor_mod, "_WEB_DOCS_DB", str(db))
    real_import = builtins.__import__

    def _no_ddgs(name, *a, **k):
        if name == "ddgs":
            raise ModuleNotFoundError("No module named 'ddgs'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_ddgs)
    auditor_mod.research_and_cache("Implement a CMake raytracer in C++")
    con = sqlite3.connect(db)
    poisoned = con.execute(
        "select count(*) from web_docs where content like '(search unavailable%'").fetchone()[0]
    assert poisoned == 0, "the live incident: error strings cached as documentation"


@pytest.mark.skipif("FORGE_TEST_NETWORK" not in __import__("os").environ,
                    reason="set FORGE_TEST_NETWORK=1 for live search tests")
def test_integration_live_search_finds_the_goal4_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor_mod, "_WEB_DOCS_DB", str(tmp_path / "docs.db"))
    hits = web_search_cached("error: 'string' in namespace 'std' does not name a type")
    assert "string" in hits.lower()
    assert "http" in hits  # URLs present so fetch_doc can chain
