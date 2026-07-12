"""Pins forge_runtime/context_compactor.py (executor prompt autocompaction +
Ollama KV pruning) and the local fallback in app/context_compression.py.

Live incidents motivating these:
- Every executor turn logged "Headroom fallback: ModuleNotFoundError: No
  module named 'headroom'" — the compression layer was a no-op in production.
- Ollama KV cache at num_ctx=200k grew 7.7GB -> 17GB over a session, ending
  in host swap exhaustion that wedged the Docker daemon (2026-07-09).
"""
import json
from unittest import mock

from forge_runtime.context_compactor import (
    compact_messages,
    estimate_tokens,
    kv_prune_interval,
    prune_ollama_kv,
)


def _msgs(n_tool_turns: int, payload: str) -> list[dict]:
    msgs = [{"role": "user", "content": "SYSTEM PACK: goal, tests, toolkit"}]
    for i in range(n_tool_turns):
        msgs.append({"role": "assistant", "content": json.dumps(
            {"type": "tool_call", "name": "bash", "arguments": {"command": f"step {i}"}})})
        msgs.append({"role": "user", "content": f"Tool execution result:\n{payload}"})
    return msgs


# ---- compact_messages --------------------------------------------------------

def test_under_threshold_is_untouched():
    msgs = _msgs(2, "ok\n" * 10)
    out, report = compact_messages(msgs, threshold_tokens=100_000, keep_recent=4)
    assert out is msgs
    assert report["compacted"] is False


def test_over_threshold_folds_middle_keeps_head_and_tail():
    payload = ("noise line about nothing\n" * 200
               + "error: assertion failed in solver.py\n")
    msgs = _msgs(10, payload)
    out, report = compact_messages(msgs, threshold_tokens=500, keep_recent=4)
    assert report["compacted"] is True
    assert out[0] == msgs[0]                      # system pack verbatim
    assert out[-4:] == msgs[-4:]                  # working set verbatim
    assert len(out) == 1 + 1 + 4                  # head + summary + tail
    assert report["tokens_after"] < report["tokens_before"]


def test_folded_summary_retains_failure_evidence_and_paths():
    payload = ("some banner text here\n" * 50
               + "error: undefined reference in raytracer.cpp\nexit code 2\n")
    msgs = _msgs(8, payload)
    out, report = compact_messages(msgs, threshold_tokens=500, keep_recent=2)
    assert report["compacted"]
    summary = out[1]["content"]
    assert "raytracer.cpp" in summary
    assert "exit code 2" in summary
    assert summary.count("some banner text here") < 8 * 50  # noise actually dropped


def test_salvage_never_returns_blank_for_pure_noise():
    msgs = _msgs(8, "lorem ipsum banner\n" * 100)
    out, report = compact_messages(msgs, threshold_tokens=500, keep_recent=2)
    assert report["compacted"]
    assert "lorem ipsum banner" in out[1]["content"]  # head/tail kept, not blank


def test_env_threshold_is_honored(monkeypatch):
    monkeypatch.setenv("PGE_CONTEXT_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("PGE_CONTEXT_COMPACT_KEEP", "2")
    msgs = _msgs(6, "error: x failed\n" * 5)
    out, report = compact_messages(msgs)
    assert report["compacted"] is True
    assert out[-2:] == msgs[-2:]


def test_estimate_tokens_scales_with_content():
    small = estimate_tokens([{"role": "user", "content": "hi"}])
    big = estimate_tokens([{"role": "user", "content": "x" * 40_000}])
    assert big > small
    assert 9_000 <= big <= 11_000  # ~chars/4


# ---- KV pruning --------------------------------------------------------------

def test_kv_prune_interval_env(monkeypatch):
    monkeypatch.setenv("PGE_KV_PRUNE_INTERVAL", "0")
    assert kv_prune_interval() == 0
    monkeypatch.setenv("PGE_KV_PRUNE_INTERVAL", "7")
    assert kv_prune_interval() == 7
    monkeypatch.setenv("PGE_KV_PRUNE_INTERVAL", "junk")
    assert kv_prune_interval() == 4


def test_prune_posts_keep_alive_zero_to_native_api(monkeypatch):
    captured = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp()

    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "gemma4:12b-mlx")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    with mock.patch("urllib.request.urlopen", fake_urlopen):
        assert prune_ollama_kv() is True
    assert captured["url"] == "http://localhost:11434/api/generate"  # /v1 stripped
    assert captured["body"] == {"model": "gemma4:12b-mlx", "keep_alive": 0}


def test_prune_never_raises_when_daemon_is_down(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:12b-mlx")
    with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert prune_ollama_kv() is False  # optimization, not a failure mode


def test_prune_noops_without_a_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert prune_ollama_kv(model=None) is False


# ---- local fallback in app/context_compression.py -----------------------------

def _fallback_pack(bulk_size: int) -> dict:
    return {
        "PROJECT": {"goal": "build the thing"},
        "ACTIVE_TASK": {"title": "task"},
        "MEMORY_EVIDENCE": [],
        "LESSONS_AND_MISTAKES": [{"note": f"lesson {i} " + "x" * 50}
                                 for i in range(bulk_size)],
    }


def test_headroom_missing_no_longer_reports_a_raw_error():
    from app.context_compression import compress_context_pack
    # headroom is not installed in this environment — exactly the live incident.
    out = compress_context_pack(None, project_id="p", goal_id=None, task_id=None,
                                pack=_fallback_pack(2))
    meta = out["CONTEXT_COMPRESSION"]
    assert meta["status"] in ("not_needed", "local_compacted")
    assert "compressor" in meta


def test_local_fallback_compacts_oversized_bulk(monkeypatch):
    from app.context_compression import compress_context_pack
    monkeypatch.setenv("PGE_CONTEXT_COMPACT_THRESHOLD", "100")  # ~400 chars
    out = compress_context_pack(None, project_id="p", goal_id=None, task_id=None,
                                pack=_fallback_pack(50))
    meta = out["CONTEXT_COMPRESSION"]
    assert meta["status"] == "local_compacted"
    assert meta["chars_after"] < meta["chars_before"]
    # Newest lessons survive; oldest are the ones folded.
    kept = out["LESSONS_AND_MISTAKES"]
    assert kept
    assert kept[-1]["note"].startswith("lesson 49")
    # Protected keys never touched.
    assert out["PROJECT"] == {"goal": "build the thing"}
