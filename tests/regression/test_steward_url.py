"""Pins the steward's base-URL resolution.

Live incident (2026-07-09, ML v5 run): STEWARD_URL was hardcoded to LM
Studio (127.0.0.1:1234) while the rest of the stack ran on Ollama via
LLM_BASE_URL — port 1234 answered HTTP 400 "No models loaded" on every
turn, silently degrading every executor briefing to the raw fallback.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))


def _reload_steward():
    import src.steward as steward
    return importlib.reload(steward)


def test_steward_url_follows_llm_base_url(monkeypatch):
    monkeypatch.delenv("PGE_STEWARD_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    assert _reload_steward().STEWARD_URL == "http://localhost:11434/v1"


def test_steward_url_env_override_wins(monkeypatch):
    monkeypatch.setenv("PGE_STEWARD_BASE_URL", "http://localhost:1235/v1/")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    # Trailing slash stripped so f"{URL}/chat/completions" stays well-formed.
    assert _reload_steward().STEWARD_URL == "http://localhost:1235/v1"


def test_steward_url_default_without_env(monkeypatch):
    monkeypatch.delenv("PGE_STEWARD_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert _reload_steward().STEWARD_URL == "http://127.0.0.1:1234/v1"
