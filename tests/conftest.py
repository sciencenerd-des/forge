"""Shared pytest setup for Forge.

Puts the two package roots (repo root for ``app``/``control_plane``/``forge_config``
and ``engine`` for ``src``/``hermes_tools``) on ``sys.path`` and points
``FORGE_HOME`` at a throwaway temp dir so tests never touch real state.
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ENGINE = _ROOT / "engine"
for p in (str(_ROOT), str(_ENGINE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Isolate all filesystem state into a temp home for the whole test session.
os.environ.setdefault("FORGE_HOME", tempfile.mkdtemp(prefix="forge-test-home-"))
