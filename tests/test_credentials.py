import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def test_provider_resolution_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_MODEL", "global-model")

    import forge_config
    from forge_runtime.credentials import save

    save({"version": 1, "profiles": {
        "default": {"base_url": "http://default/v1", "model": "file-default", "api_key": "file-key"},
        "executor": {"model": "file-executor", "api_key": "executor-key"},
    }})
    monkeypatch.setenv("PGE_EXECUTOR_MODEL", "env-executor")
    monkeypatch.setenv("FORGE_EXECUTOR_API_KEY", "env-key")

    result = forge_config.provider_for("executor")

    assert result["model"] == "env-executor"
    assert result["api_key"] == "env-key"
    assert result["base_url"] == "http://default/v1"


def test_masked_profile_never_contains_raw_key():
    from forge_runtime.credentials import masked_profile

    result = masked_profile({"api_key": "sk-live-secret-1234", "model": "m"})

    assert result["api_key"] != "sk-live-secret-1234"
    assert result["api_key"].endswith("1234")


def test_corrupt_store_is_rejected_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    (tmp_path / "providers.json").write_text("not json")

    from forge_runtime.credentials import load

    try:
        load()
    except ValueError as exc:
        assert "invalid providers.json" in str(exc)
    else:
        raise AssertionError("corrupt provider store was accepted")


def test_connection_reports_reachable_models():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "model-a"}]}).encode())

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from forge_runtime.credentials import test_connection

        result = test_connection({"base_url": f"http://127.0.0.1:{server.server_port}/v1"})
        assert result == {"status": "reachable", "models": ["model-a"]}
    finally:
        server.shutdown()
        thread.join(timeout=2)
