import json

import forge_config
from forge_runtime.llm import extract_json


def test_extract_json_handles_fences_and_prefix():
    assert json.loads(extract_json('json\n```json\n{"ok": true}\n```')) == {"ok": True}


def test_ollama_dialect_auto_detection(monkeypatch):
    monkeypatch.delenv("FORGE_LLM_DIALECT", raising=False)
    monkeypatch.setenv("FORGE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert forge_config.llm_dialect("executor") == "ollama"


def test_dialect_can_be_forced(monkeypatch):
    monkeypatch.setenv("FORGE_LLM_DIALECT", "openai")
    assert forge_config.llm_dialect("executor") == "openai"
