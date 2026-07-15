import json

import forge_config
from forge_runtime.llm import extract_json


def test_extract_json_handles_fences_and_prefix():
    assert json.loads(extract_json('json\n```json\n{"ok": true}\n```')) == {"ok": True}


def test_extract_json_unwraps_junk_wrapped_fence():
    # Gemma-on-Ollama produced a fake outer `{"` before the real fenced object;
    # the brace-window fallback grabbed the malformed wrapper. Regression for the
    # 0/8 eval. See llm.extract_json.
    raw = 'json\n{"```json\n{\n  "type": "tool_call",\n  "name": "read_file",\n  "arguments": {"path": "src/app.py"}\n}\n```'
    assert json.loads(extract_json(raw)) == {
        "type": "tool_call", "name": "read_file", "arguments": {"path": "src/app.py"}}


def test_extract_json_from_clean_object_and_trailing_prose():
    assert json.loads(extract_json('{"type": "heartbeat"}')) == {"type": "heartbeat"}
    assert json.loads(extract_json('```\n{"a": 1}\n``` done')) == {"a": 1}


def test_ollama_dialect_auto_detection(monkeypatch):
    monkeypatch.delenv("FORGE_LLM_DIALECT", raising=False)
    monkeypatch.setenv("FORGE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert forge_config.llm_dialect("executor") == "ollama"


def test_dialect_can_be_forced(monkeypatch):
    monkeypatch.setenv("FORGE_LLM_DIALECT", "openai")
    assert forge_config.llm_dialect("executor") == "openai"
