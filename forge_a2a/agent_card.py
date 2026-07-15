from __future__ import annotations

import os


def agent_card() -> dict:
    return {
        "name": "Forge",
        "description": "Long-horizon coding task execution with test-verified completion.",
        "supportedInterfaces": [{"url": f"{os.getenv('FORGE_CONTROL_URL', 'http://127.0.0.1:8787').rstrip('/')}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "version": "0.1.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [{"id": "autonomous-coding", "name": "Autonomous coding", "description": "Execute a scoped coding goal and report verified artifacts.", "tags": ["coding"], "inputModes": ["text/plain"], "outputModes": ["text/plain", "application/json"]}],
        "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer", "description": "Forge control-plane bearer token"}}},
        "securityRequirements": [{"schemes": {"bearer": []}}],
    }
