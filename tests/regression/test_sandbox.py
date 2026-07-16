"""Pins forge_runtime/sandbox.py: the default per-project execution
environment (see docker-compose.yml's otel-collector/docker.sock wiring and
docker/sandbox/Dockerfile for the container image).

Runs entirely without Docker in CI (degradation path), matching the existing
pattern in test_hermetic_replay.py. Real container behavior — non-root
execution, isolation from the host filesystem, and the run_tests/lint
exit-code-masking fixes — is exercised only when FORGE_TEST_DOCKER=1 and
docker is present.
"""
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from forge_runtime.sandbox import (
    ContainerSandbox,
    HostWorkspace,
    SandboxError,
    docker_available,
    get_workspace,
    reset_workspace_cache,
    sandbox_mode,
)
from forge_runtime.sandbox import _stage_dir_snapshot as stage_dir_snapshot
from forge_runtime.tools import ToolContext, ToolRequest, default_registry


def test_sandbox_mode_defaults_to_container():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FORGE_SANDBOX_MODE", None)
        assert sandbox_mode() == "container"


def test_sandbox_mode_env_override_to_host():
    with mock.patch.dict(os.environ, {"FORGE_SANDBOX_MODE": "host"}):
        assert sandbox_mode() == "host"


def test_sandbox_mode_unrecognized_value_defaults_to_container():
    with mock.patch.dict(os.environ, {"FORGE_SANDBOX_MODE": "nonsense"}):
        assert sandbox_mode() == "container"


def test_get_workspace_fails_closed_when_docker_unavailable(tmp_path: Path):
    reset_workspace_cache()
    with mock.patch("forge_runtime.sandbox.docker_available", return_value=False):
        with pytest.raises(SandboxError, match="container sandbox is required"):
            get_workspace("fallback-project-1", str(tmp_path))
    reset_workspace_cache()


def test_get_workspace_falls_back_to_host_when_mode_is_host(tmp_path: Path):
    reset_workspace_cache()
    with mock.patch.dict(os.environ, {"FORGE_SANDBOX_MODE": "host"}):
        ws = get_workspace("fallback-project-2", str(tmp_path))
    assert isinstance(ws, HostWorkspace)
    reset_workspace_cache()


def test_get_workspace_caches_per_project(tmp_path: Path):
    reset_workspace_cache()
    with mock.patch.dict(os.environ, {"FORGE_SANDBOX_MODE": "host"}):
        first = get_workspace("cache-project", str(tmp_path))
        second = get_workspace("cache-project", str(tmp_path))
    assert first is second
    reset_workspace_cache()


def test_get_workspace_fails_closed_when_container_creation_raises(tmp_path: Path):
    reset_workspace_cache()
    with mock.patch("forge_runtime.sandbox.docker_available", return_value=True), \
         mock.patch("forge_runtime.sandbox.ContainerSandbox.__init__",
                     side_effect=RuntimeError("docker daemon unreachable")):
        with pytest.raises(SandboxError, match="container sandbox initialization failed"):
            get_workspace("broken-docker-project", str(tmp_path))
    reset_workspace_cache()


def test_host_workspace_read_write_roundtrip(tmp_path: Path):
    ws = HostWorkspace(str(tmp_path))
    ws.write_text("a/b.txt", "hello")
    assert ws.read_text("a/b.txt") == "hello"
    assert ws.exists("a/b.txt") and ws.is_file("a/b.txt")
    assert "a/b.txt" in ws.list_files()


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt"])
def test_host_workspace_rejects_paths_outside_workspace(tmp_path: Path, path: str):
    ws = HostWorkspace(str(tmp_path))
    with pytest.raises(ValueError, match="workspace"):
        ws.write_text(path, "blocked")


def test_container_workspace_path_rejects_traversal_and_absolute_paths():
    sandbox = object.__new__(ContainerSandbox)
    with pytest.raises(ValueError, match="workspace"):
        sandbox._p("../../etc/passwd")
    with pytest.raises(ValueError, match="absolute"):
        sandbox._p("/etc/passwd")


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt"])
def test_tool_registry_rejects_host_sandbox_path_escapes(tmp_path: Path, path: str):
    context = ToolContext(workspace=tmp_path, sandbox=HostWorkspace(str(tmp_path)))
    result = default_registry().execute(
        context, ToolRequest("write_file", {"path": path, "content": "blocked"}),
    )
    assert not result.ok
    assert "workspace" in (result.error or "") or "absolute" in (result.error or "")


def test_tool_registry_rejects_host_sandbox_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("original")
    (tmp_path / "link").symlink_to(outside)
    context = ToolContext(workspace=tmp_path, sandbox=HostWorkspace(str(tmp_path)))
    result = default_registry().execute(
        context, ToolRequest("write_file", {"path": "link", "content": "blocked"}),
    )
    assert not result.ok
    assert outside.read_text() == "original"


def test_sync_to_host_preserves_original_on_docker_copy_failure(tmp_path: Path):
    host = tmp_path / "workspace"
    host.mkdir()
    (host / "keep.txt").write_text("original")
    sandbox = object.__new__(ContainerSandbox)
    sandbox.container_name = "fake-container"
    failed = mock.Mock(returncode=1, stdout="", stderr="daemon unavailable")
    with mock.patch("forge_runtime.sandbox.subprocess.run", return_value=failed):
        with pytest.raises(SandboxError, match="host sync failed"):
            sandbox.sync_to_host(str(host))
    assert (host / "keep.txt").read_text() == "original"


def test_host_import_failure_preserves_original_workspace(tmp_path: Path, monkeypatch):
    host = tmp_path / "workspace"
    host.mkdir()
    (host / "keep.txt").write_text("original")
    sandbox = object.__new__(ContainerSandbox)
    sandbox.container_name = "fake-container"
    sandbox.volume_name = "fake-volume"
    sandbox.image = "fake-image"
    sandbox.host_path = host

    def fake_exec(self, argv, cwd=None, timeout=60, env=None):
        if argv[:2] == ["test", "-f"]:
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        if argv[:1] == ["find"]:
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        raise AssertionError(f"unexpected command: {argv}")

    sandbox._exec = fake_exec.__get__(sandbox, ContainerSandbox)
    failed = mock.Mock(returncode=1, stdout="", stderr="daemon unavailable")
    with mock.patch("forge_runtime.sandbox.subprocess.run", return_value=failed):
        with pytest.raises(SandboxError, match="host import failed"):
            sandbox._import_host_workspace_once()
    assert (host / "keep.txt").read_text() == "original"


def test_host_workspace_run_executes_shell(tmp_path: Path):
    ws = HostWorkspace(str(tmp_path))
    result = ws.run("echo hi")
    assert result.returncode == 0
    assert "hi" in result.stdout


def test_host_workspace_write_is_atomic_no_partial_file_on_crash(tmp_path: Path):
    # Matches forge_runtime/tools.py write_file's tmp-then-rename contract.
    ws = HostWorkspace(str(tmp_path))
    ws.write_text("f.txt", "v1")
    ws.write_text("f.txt", "v2")
    assert ws.read_text("f.txt") == "v2"
    assert not list(tmp_path.glob(".*.forge-tmp-*"))


def test_export_snapshot_strips_prune_dirs_and_is_content_addressed(tmp_path: Path):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (src / "main.py").write_text("print('hi')\n")
    ws = HostWorkspace(str(src))

    snap = ws.export_snapshot(str(tmp_path / "snap"))
    staged = Path(snap.path)
    assert (staged / "main.py").exists()
    assert not (staged / ".git").exists()          # VCS metadata pruned
    assert not (staged / "__pycache__").exists()    # caches pruned
    assert snap.file_count == 1

    # Byte-identical content -> identical digest; a change -> different digest.
    snap_again = ws.export_snapshot(str(tmp_path / "snap2"))
    assert snap_again.digest == snap.digest
    (src / "main.py").write_text("print('changed')\n")
    snap_changed = HostWorkspace(str(src)).export_snapshot(str(tmp_path / "snap3"))
    assert snap_changed.digest != snap.digest


def test_stage_dir_snapshot_empty_workspace_has_stable_digest(tmp_path: Path):
    a = _make_empty(tmp_path / "a")
    b = _make_empty(tmp_path / "b")
    assert stage_dir_snapshot(a, tmp_path / "sa").digest == stage_dir_snapshot(b, tmp_path / "sb").digest


@pytest.mark.parametrize("relative", [False, True])
def test_stage_dir_snapshot_never_follows_symlinks(tmp_path: Path, relative: bool):
    src = _make_empty(tmp_path / "src")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("host secret")
    target = Path("../outside-secret.txt") if relative else outside
    (src / "leak.txt").symlink_to(target)

    snapshot = stage_dir_snapshot(src, tmp_path / "snapshot")

    assert not (Path(snapshot.path) / "leak.txt").exists()
    assert snapshot.file_count == 0


def _make_empty(p: Path) -> Path:
    p.mkdir(parents=True)
    return p


_DOCKER_GATE = pytest.mark.skipif(
    os.environ.get("FORGE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set FORGE_TEST_DOCKER=1 with Docker available to run real sandbox container tests",
)


@_DOCKER_GATE
def test_integration_container_runs_as_non_root_with_no_capabilities():
    assert docker_available()
    reset_workspace_cache()
    project_id = "sandboxtest-nonroot"
    ws = get_workspace(project_id, "/tmp/unused-host-path")
    try:
        assert isinstance(ws, ContainerSandbox)
        r = ws.run("id -u")
        assert r.returncode == 0
        assert r.stdout.strip() == "10001", "sandbox must run as the fixed non-root UID"
        # Every capability dropped: chown as a non-root, non-CAP_CHOWN user
        # to a file it doesn't own must fail.
        r2 = ws.run("touch /tmp/x && chown 1:1 /tmp/x 2>&1; echo EXIT:$?")
        assert "EXIT:0" not in r2.stdout, "chown should fail with all capabilities dropped"
    finally:
        ws.remove()
        reset_workspace_cache()


@_DOCKER_GATE
def test_integration_container_imports_host_workspace_once_and_keeps_later_writes_isolated():
    assert docker_available()
    reset_workspace_cache()
    with tempfile.TemporaryDirectory() as host_dir:
        marker = Path(host_dir) / "host-source.txt"
        marker.write_text("must be imported before first use")
        project_id = "sandboxtest-isolation"
        ws = get_workspace(project_id, host_dir)
        try:
            assert isinstance(ws, ContainerSandbox)
            assert ws.read_text("host-source.txt") == "must be imported before first use"
            probe = ws.run_in_empty_scratch("test ! -e host-source.txt")
            assert probe.returncode == 0, "contract probes must not see the imported workspace"
            ws.write_text("in-container.txt", "written from inside the sandbox")
            # The host directory must NOT gain the file the sandbox wrote —
            # there is no bind mount, only a Docker-managed named volume.
            assert not (Path(host_dir) / "in-container.txt").exists()
            # The one-time import is complete; subsequent host changes do not
            # silently overwrite the running sandbox workspace.
            marker.write_text("host changed after import")
            assert ws.read_text("host-source.txt") == "must be imported before first use"
        finally:
            ws.remove()
            reset_workspace_cache()


@_DOCKER_GATE
def test_integration_container_persists_across_get_workspace_calls():
    assert docker_available()
    reset_workspace_cache()
    project_id = "sandboxtest-persist"
    ws1 = get_workspace(project_id, "/tmp/unused-host-path")
    try:
        ws1.write_text("persisted.txt", "still here")
        reset_workspace_cache()  # simulate a fresh process picking the project back up
        ws2 = get_workspace(project_id, "/tmp/unused-host-path")
        assert ws2.container_name == ws1.container_name
        assert ws2.read_text("persisted.txt") == "still here"
    finally:
        ws1.remove()
        reset_workspace_cache()


@_DOCKER_GATE
def test_integration_sync_to_host_replaces_only_after_a_staged_copy():
    assert docker_available()
    reset_workspace_cache()
    with tempfile.TemporaryDirectory() as host_dir:
        host = Path(host_dir)
        (host / "source.txt").write_text("host source")
        ws = get_workspace("sandboxtest-transactional-sync", host_dir)
        try:
            ws.write_text("result.txt", "container result")
            with ws.copied_workspace_for_probe() as probe:
                ws.remove_files_from_probe(probe, ["source.txt"])
                assert ws.run("test ! -e source.txt", cwd=ws._p(probe)).returncode == 0
                assert ws.run("test -f source.txt", cwd="/workspace").returncode == 0
            ws.sync_to_host(host_dir)
            assert (host / "result.txt").read_text() == "container result"
            assert not (host / ".git").exists()
            assert not (host / ".forge-sandbox-host-import-v1").exists()
            assert not (host / ".forge-internal").exists()
        finally:
            ws.remove()
            reset_workspace_cache()


def test_sandbox_creation_includes_resource_limits(monkeypatch):
    """Live incident (2026-07-09, ML experiment-loop goal): an unbounded
    in-sandbox sklearn training loop pinned the Docker VM at 300%+ CPU and
    exhausted host swap — `docker exec` and even `docker ps` hung, so every
    audit test reported runner errors: the verification infrastructure was
    wedged by the very workload it existed to judge. Privilege isolation
    (cap-drop, non-root) and resource isolation are different axes; the
    sandbox must carry memory/cpu/pids limits."""
    captured = []

    def fake_docker(self, argv, timeout=60):
        captured.append(argv)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(ContainerSandbox, "_docker", fake_docker)
    monkeypatch.setattr(ContainerSandbox, "_container_state", lambda self: None)
    ContainerSandbox("limitcheck-project")
    run_call = next(a for a in captured if a and a[0] == "run" and "-d" in a)
    joined = " ".join(run_call)
    assert "--memory" in joined
    assert "--memory-swap" in joined
    assert "--cpus" in joined
    assert "--pids-limit" in joined
    # And the privilege hardening must still be intact alongside.
    assert "--cap-drop" in joined and "no-new-privileges" in joined


def test_container_run_prepends_workspace_venv_to_path(monkeypatch):
    """Live incident (2026-07-09, ML v5 run): install_deps installed pandas
    into /workspace/.venv, but the bash tool's `python main.py` resolved the
    SYSTEM python — ModuleNotFoundError four batches in a row. Host mode
    already fixes this via _venv_env; container mode passed env=None. Every
    string command must see /workspace/.venv/bin first on PATH."""
    captured = []

    def fake_exec(self, argv, cwd=None, timeout=60, env=None):
        captured.append(argv)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(ContainerSandbox, "_docker",
                        lambda self, argv, timeout=60: type("R", (), {
                            "returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(ContainerSandbox, "_container_state", lambda self: None)
    monkeypatch.setattr(ContainerSandbox, "_exec", fake_exec)
    sb = ContainerSandbox("venvpath-project")
    sb.run("python main.py")
    shell_cmd = captured[-1][-1]
    assert '/workspace/.venv/bin' in shell_cmd.split("python main.py")[0]
    assert shell_cmd.endswith("python main.py")

    # argv-form commands get the same PATH prepend — a bare `docker exec`
    # never consults a shell, so ["python3", "-c", ...] silently kept the
    # default PATH (live incident, 2026-07-09 REST API run: fastapi installed
    # in /workspace/.venv, argv-form `python3 -c "from main import app"`
    # failed with ModuleNotFoundError: fastapi).
    sb.run(["python3", "-c", "from main import app"])
    argv = captured[-1]
    assert argv[:2] == ["/bin/bash", "-lc"]
    shell_cmd = argv[-1]
    assert '/workspace/.venv/bin' in shell_cmd.split("python3")[0]
    assert shell_cmd.endswith("python3 -c 'from main import app'")
