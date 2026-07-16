"""Container sandbox: the default execution environment for a project's
workspace.

Every tool the executor calls (write_file, read_file, bash, install_deps,
run_tests, ...) needs somewhere to actually run. Historically that was the
host filesystem directly, gated only by ``FORGE_ALLOW_HOST_EXECUTION`` (see
forge_runtime/tools.py). This module makes the DEFAULT somewhere else: a
long-lived, non-root, capability-dropped Docker container per project, with
its files on a Docker-managed named volume — never a host bind-mount, so the
container has no path back to the host filesystem at all.

Two ``Workspace`` implementations share one interface (read_text/write_text/
exists/is_file/list_files/run):

- ``HostWorkspace`` — direct host-path behavior, available only when an
  operator explicitly sets ``FORGE_SANDBOX_MODE=host``.  Container mode fails
  closed when Docker is unavailable or sandbox initialization fails.
- ``ContainerSandbox`` — the default. A persistent per-project container
  (``forge-sandbox-<project_id[:12]>``) that:
    * runs every command as a fixed non-root UID/GID (10001:10001)
    * has every Linux capability dropped (``--cap-drop=ALL``) and
      ``no-new-privileges`` set — even a compromised process inside it
      cannot escalate or use dropped capabilities.
    * mounts its workspace from a Docker-managed named volume, NOT a host
      bind-mount — code the executor writes lives only in that volume; the
      container has no visibility into the host filesystem whatsoever.
    * is created via the ``docker`` CLI (subprocess), matching the pattern
      already established by ``verifier_hierarchy.docker_available()`` —
      no new SDK dependency.

One root-adjacent detail, documented rather than hidden: the named volume
Docker creates defaults to root ownership, so a ONE-TIME bootstrap step
(``docker run --rm ... chown -R 10001:10001 /workspace``) runs as root
briefly, before the persistent sandbox container ever starts, purely to fix
volume permissions on an empty directory. The persistent sandbox the
executor actually operates in NEVER runs as root and is never granted that
bootstrap container's privileges.
"""
from __future__ import annotations

import base64
import os
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional, Sequence, Union

SANDBOX_IMAGE = os.getenv("FORGE_SANDBOX_IMAGE", "forge-sandbox:latest")
SANDBOX_UID = 10001
SANDBOX_GID = 10001
_DEFAULT_TIMEOUT = 120

# Directories excluded from an exported snapshot: VCS metadata, our own probes,
# build caches, and dependency trees. They are run byproducts, not the artifact,
# and copying them would let stale/agent-controlled state pollute verification.
_SNAPSHOT_PRUNE = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                   ".ruff_cache", "node_modules", ".venv", "venv", ".tox",
                   "target", "build", ".forge-sandbox-host-import-v1"}


@dataclass(frozen=True)
class SnapshotResult:
    """An immutable, content-addressed copy of a workspace at a terminal moment."""

    path: str
    digest: str
    file_count: int


def _stage_dir_snapshot(src: Path, dest: Path) -> SnapshotResult:
    """Copy ``src`` -> ``dest`` minus prune dirs and compute a content digest.

    The digest is over the sorted (relative-path, sha256(bytes)) pairs, so it is
    stable across machines and independent of mtimes — two runs that produced
    byte-identical artifacts get the same digest.
    """
    import hashlib

    dest.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, str]] = []
    for path in sorted(src.rglob("*")):
        if any(part in _SNAPSHOT_PRUNE for part in path.relative_to(src).parts):
            continue
        # The workspace is untrusted. ``is_file`` and ``read_bytes`` follow
        # symlinks, which would turn a link to a host file into verifier input.
        # A snapshot contains only regular files physically rooted under src.
        if path.is_symlink():
            continue
        rel = path.relative_to(src)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        target.write_bytes(data)
        entries.append((str(rel), hashlib.sha256(data).hexdigest()))
    digest = hashlib.sha256(
        "\0".join(f"{rel}:{h}" for rel, h in entries).encode("utf-8")
    ).hexdigest()
    return SnapshotResult(path=str(dest), digest=digest, file_count=len(entries))


def docker_available() -> bool:
    """Same contract as verifier_hierarchy.docker_available(): CLI present
    (a daemon-reachability check happens lazily on first real command, so an
    installed-but-stopped Docker Desktop degrades on first use, not here)."""
    return shutil.which("docker") is not None


def sandbox_mode() -> str:
    """"container" (default) or "host" — FORGE_SANDBOX_MODE env override.
    Any other/unset value is treated as "container"."""
    mode = os.getenv("FORGE_SANDBOX_MODE", "container").strip().lower()
    return "host" if mode == "host" else "container"


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SandboxError(ValueError):
    pass


class Workspace:
    """Common interface HostWorkspace and ContainerSandbox both implement.
    ``root`` is a display-only path used in prompts/logging — callers never
    need to know which implementation they're holding."""

    root: str

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def write_text(self, path: str, content: str) -> None:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def is_file(self, path: str) -> bool:
        raise NotImplementedError

    def list_files(self, path: str = ".", limit: int = 200) -> list[str]:
        raise NotImplementedError

    def run(self, command: Union[str, Sequence[str]], cwd: Optional[str] = None,
            timeout: int = _DEFAULT_TIMEOUT, env: Optional[dict] = None) -> CommandResult:
        raise NotImplementedError

    def sync_to_host(self, host_dir: str) -> None:
        """Mirror this workspace's content into a plain host directory, for
        the benefit of consumers that only know how to read Path objects
        (forge_runtime/contract_immune.py's mutation/vacuous-test/overfitting/
        scope-completeness gates and semantic-flag hashing — rewriting that
        whole module to be sandbox-aware was judged higher-risk than syncing
        once per evaluation cycle). No-op for HostWorkspace, which already IS
        the host directory."""
        raise NotImplementedError

    def export_snapshot(self, dest: str) -> SnapshotResult:
        """Stage an immutable, content-addressed copy of this workspace.

        This is the *authoritative* artifact source for acceptance: in container
        mode the real workspace lives in the container's volume, so reading the
        host mirror (which may hold only ``.git``) would verify stale bytes. The
        caller must invoke this only after the run is terminal; the export never
        overwrites the project's host mirror. Prune dirs are stripped and a
        content digest is returned so a result can record exactly what was
        judged."""
        raise NotImplementedError


class HostWorkspace(Workspace):
    """Direct host-filesystem/subprocess execution — today's pre-sandbox
    behavior, preserved exactly for FORGE_SANDBOX_MODE=host and as the
    automatic fallback when Docker is unavailable."""

    def __init__(self, root: str):
        self._root = Path(root).expanduser().resolve(strict=False)
        self.root = str(self._root)

    def _abs(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty workspace-relative string")
        raw = Path(path)
        if raw.is_absolute():
            raise ValueError("absolute paths are not allowed in a workspace")
        candidate = (self._root / raw).resolve(strict=False)
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise ValueError("path escapes the configured workspace") from error
        return candidate

    def read_text(self, path: str) -> str:
        return self._abs(path).read_text(encoding="utf-8", errors="replace")

    def write_text(self, path: str, content: str) -> None:
        target = self._abs(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.forge-tmp-{os.getpid()}")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)

    def exists(self, path: str) -> bool:
        return self._abs(path).exists()

    def is_file(self, path: str) -> bool:
        return self._abs(path).is_file()

    def list_files(self, path: str = ".", limit: int = 200) -> list[str]:
        root = self._abs(path)
        entries = []
        skip = {".git", "node_modules", "target", ".venv"}
        iterator = root.rglob("*") if root.is_dir() else [root]
        for p in iterator:
            if len(entries) >= limit:
                break
            if any(part in skip for part in p.parts):
                continue
            entries.append(str(p.relative_to(self.root)) if p.is_absolute() else str(p))
        return entries

    def run(self, command, cwd: Optional[str] = None, timeout: int = _DEFAULT_TIMEOUT,
            env: Optional[dict] = None) -> CommandResult:
        argv = ["/bin/bash", "-lc", command] if isinstance(command, str) else list(command)
        proc = subprocess.run(argv, cwd=cwd or self.root, timeout=timeout,
                               capture_output=True, text=True, env=env)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def sync_to_host(self, host_dir: str) -> None:
        pass  # already the host directory

    def export_snapshot(self, dest: str) -> SnapshotResult:
        return _stage_dir_snapshot(self._root, Path(dest).expanduser().resolve(strict=False))


class ContainerSandbox(Workspace):
    """Long-lived, non-root, capability-dropped per-project container."""

    _HOST_IMPORT_MARKER = ".forge-sandbox-host-import-v1"

    def __init__(self, project_id: str, host_path: str | None = None, image: str = SANDBOX_IMAGE):
        self.project_id = project_id
        self.image = image
        self.container_name = f"forge-sandbox-{project_id[:12]}"
        self.volume_name = f"forge-ws-{project_id[:12]}"
        self.root = "/workspace"
        self.host_path = (Path(host_path).expanduser().resolve(strict=False)
                          if host_path is not None else None)
        self._ensure_running()

    # -- lifecycle -----------------------------------------------------

    def _docker(self, argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout)

    def _container_state(self) -> Optional[str]:
        r = self._docker(["inspect", "-f", "{{.State.Status}}", self.container_name])
        return r.stdout.strip() if r.returncode == 0 else None

    def _ensure_running(self) -> None:
        state = self._container_state()
        if state == "running":
            return
        if state is not None:
            # Exists but stopped (e.g. host restarted) — restart in place,
            # the named volume (and everything the executor wrote) is untouched.
            r = self._docker(["start", self.container_name])
            if r.returncode == 0:
                return
            # Couldn't restart (e.g. image pruned) — remove and recreate below.
            self._docker(["rm", "-f", self.container_name])

        self._docker(["volume", "create", self.volume_name])
        # One-time root bootstrap: fix the fresh volume's ownership so the
        # persistent (non-root) sandbox below can actually write to it. This
        # ephemeral container runs, chowns an EMPTY directory, and exits —
        # it never executes any executor/LLM-directed command.
        chown = self._docker([
            "run", "--rm", "-v", f"{self.volume_name}:/workspace",
            self.image, "chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", "/workspace",
        ], timeout=120)
        if chown.returncode != 0:
            raise SandboxError(f"sandbox volume bootstrap failed: {chown.stderr[:300]}")

        readonly_root = os.getenv("FORGE_SANDBOX_READONLY_ROOTFS", "").strip().lower() in {"1", "true", "yes"}
        run_argv = [
            "run", "-d", "--name", self.container_name,
            "--user", f"{SANDBOX_UID}:{SANDBOX_GID}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,size=1g,mode=1777",
            "-v", f"{self.volume_name}:/workspace",
            "-w", "/workspace",
            "-e", "HOME=/workspace/.forge-home",
            "--network", os.getenv("FORGE_SANDBOX_NETWORK", "bridge"),
            "--restart", "unless-stopped",
            # RESOURCE LIMITS — isolation is not only about privileges.
            # Reproduced live (2026-07-09, ML experiment-loop goal): an
            # unbounded sklearn training loop inside the sandbox pinned the
            # Docker VM at 300%+ CPU and exhausted host swap (15.7/17GB) on
            # a machine also carrying a ~17GB GPU-resident LLM — `docker
            # exec` (and `docker ps` itself) hung, so EVERY audit test
            # reported runner errors and the whole verification
            # infrastructure was down, wedged by the very workload it was
            # supposed to be judging. A sandbox that can starve its judge
            # isn't contained.
            "--memory", os.getenv("FORGE_SANDBOX_MEMORY", "3g"),
            "--memory-swap", os.getenv("FORGE_SANDBOX_MEMORY_SWAP", "3g"),
            "--cpus", os.getenv("FORGE_SANDBOX_CPUS", "3"),
            "--pids-limit", os.getenv("FORGE_SANDBOX_PIDS", "512"),
        ]
        if readonly_root:
            run_argv += ["--read-only"]
        run_argv += [self.image, "sleep", "infinity"]
        r = self._docker(run_argv, timeout=60)
        if r.returncode != 0:
            raise SandboxError(f"sandbox container creation failed: {r.stderr[:300]}")
        if self.host_path is not None:
            self._import_host_workspace_once()

    def _volume_has_content(self) -> bool:
        result = self._exec([
            "find", "/workspace", "-mindepth", "1", "-maxdepth", "1", "-print", "-quit",
        ])
        return bool(result.stdout.strip())

    def _import_host_workspace_once(self) -> None:
        """Seed a new sandbox volume from an existing host workspace.

        Container mode is a migration, not a fresh start.  We never choose
        between two non-empty copies silently: that ambiguity could overwrite
        user work on the later container-to-host synchronization.
        """
        marker = self._p(self._HOST_IMPORT_MARKER)
        if self._exec(["test", "-f", marker]).returncode == 0:
            return

        host_has_content = self.host_path.is_dir() and any(self.host_path.iterdir())
        volume_has_content = self._volume_has_content()
        if host_has_content and volume_has_content:
            raise SandboxError(
                "sandbox volume and host workspace both contain data but have no migration marker; "
                "resolve the divergence before continuing"
            )

        if host_has_content:
            copied = subprocess.run(
                ["docker", "cp", f"{self.host_path}/.", f"{self.container_name}:/workspace"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if copied.returncode != 0:
                raise SandboxError(f"sandbox host import failed: {copied.stderr[:300]}")
            ownership = self._docker([
                "run", "--rm", "-v", f"{self.volume_name}:/workspace",
                self.image, "chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", "/workspace",
            ], timeout=120)
            if ownership.returncode != 0:
                raise SandboxError(f"sandbox host import ownership fix failed: {ownership.stderr[:300]}")

        marked = self._exec(["touch", marker])
        if marked.returncode != 0:
            raise SandboxError(f"sandbox migration marker failed: {marked.stderr[:300]}")

    def remove(self) -> None:
        """Tear down the container AND its volume — used by tests and
        explicit project cleanup. Never called on the completion path."""
        self._docker(["rm", "-f", self.container_name])
        self._docker(["volume", "rm", "-f", self.volume_name])

    # -- Workspace interface --------------------------------------------

    def _p(self, path: str) -> str:
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        raw = PurePosixPath(path)
        if raw.is_absolute():
            raise ValueError("absolute paths are not allowed in a workspace")
        target = posixpath.normpath(f"/workspace/{raw}")
        if target != "/workspace" and not target.startswith("/workspace/"):
            raise ValueError("path escapes the configured workspace")
        return target

    def _exec(self, argv: list[str], cwd: Optional[str] = None,
              timeout: int = _DEFAULT_TIMEOUT, env: Optional[dict] = None) -> CommandResult:
        cmd = ["docker", "exec"]
        if cwd:
            cmd += ["-w", cwd]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [self.container_name, *argv]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(-1, "", f"timed out after {timeout}s")
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def read_text(self, path: str) -> str:
        r = self._exec(["cat", self._p(path)])
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or f"cannot read {path}")
        return r.stdout

    def write_text(self, path: str, content: str) -> None:
        target = self._p(path)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        script = (
            f"mkdir -p $(dirname {shlex.quote(target)}) && "
            f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(target)}"
        )
        r = self._exec(["/bin/sh", "-c", script])
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or f"cannot write {path}")

    def exists(self, path: str) -> bool:
        return self._exec(["test", "-e", self._p(path)]).returncode == 0

    def is_file(self, path: str) -> bool:
        return self._exec(["test", "-f", self._p(path)]).returncode == 0

    def list_files(self, path: str = ".", limit: int = 200) -> list[str]:
        r = self._exec([
            "find", self._p(path),
            "-not", "-path", "*/.git/*",
            "-not", "-path", "*/node_modules/*",
            "-not", "-path", "*/target/*",
            "-not", "-path", "*/.venv/*",
        ])
        if r.returncode != 0:
            return []
        root = self._p(path)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip() and ln != root][:limit]
        return [ln[len("/workspace/"):] if ln.startswith("/workspace/") else ln for ln in lines]

    def run(self, command, cwd: Optional[str] = None, timeout: int = _DEFAULT_TIMEOUT,
            env: Optional[dict] = None) -> CommandResult:
        # Project-local toolchains first on PATH, mirroring host mode's
        # _venv_env: install_deps puts packages in /workspace/.venv, so a
        # bare `python main.py` must resolve to that interpreter — the
        # observed failure was install_deps "success" followed by
        # ModuleNotFoundError because the container ran the system python.
        # Applies to BOTH command forms: argv-form (["python3", "-c", ...])
        # goes straight to `docker exec` with no shell, so it must be
        # wrapped through bash too or it silently keeps the default PATH.
        if not isinstance(command, str):
            command = " ".join(shlex.quote(a) for a in command)
        command = ('export PATH="/workspace/.venv/bin:'
                   '/workspace/node_modules/.bin:$PATH"; ' + command)
        argv = ["/bin/bash", "-lc", command]
        return self._exec(argv, cwd=cwd or self.root, timeout=timeout, env=env)

    def run_in_empty_scratch(self, command: str, timeout: int = _DEFAULT_TIMEOUT,
                             env: Optional[dict] = None) -> CommandResult:
        """Run an untrusted contract command in a fresh directory in this container."""
        relative = f".forge-internal/contract-probes/{uuid.uuid4().hex}"
        scratch = self._p(relative)
        created = self._exec(["mkdir", "-p", scratch])
        if created.returncode != 0:
            raise SandboxError(f"sandbox scratch creation failed: {created.stderr[:300]}")
        try:
            return self.run(command, cwd=scratch, timeout=timeout, env=env)
        finally:
            self._exec(["rm", "-rf", "--", scratch])

    @contextmanager
    def copied_workspace_for_probe(self) -> Iterator[str]:
        """Yield a disposable copy of the workspace inside this container.

        Probe commands remain in the container.  The copy excludes Forge's
        own scratch area so repeated probes cannot recursively clone themselves.
        """
        relative = f".forge-internal/mutation-probes/{uuid.uuid4().hex}"
        scratch = self._p(relative)
        created = self._exec(["mkdir", "-p", scratch])
        if created.returncode != 0:
            raise SandboxError(f"sandbox mutation copy creation failed: {created.stderr[:300]}")
        clone = self._exec([
            "/bin/sh", "-c",
            f"tar -C /workspace --exclude=.forge-internal -cf - . | tar -C {shlex.quote(scratch)} -xf -",
        ], timeout=120)
        if clone.returncode != 0:
            self._exec(["rm", "-rf", "--", scratch])
            raise SandboxError(f"sandbox mutation copy failed: {clone.stderr[:300]}")
        try:
            yield relative
        finally:
            self._exec(["rm", "-rf", "--", scratch])

    def remove_files_from_probe(self, probe_relative: str, paths: Sequence[str]) -> None:
        targets = [self._p(f"{probe_relative}/{path}") for path in paths]
        if not targets:
            return
        removed = self._exec(["rm", "-f", "--", *targets])
        if removed.returncode != 0:
            raise SandboxError(f"sandbox mutation deletion failed: {removed.stderr[:300]}")

    def sync_to_host(self, host_dir: str) -> None:
        """Transactionally replace the host mirror after a verified Docker copy.

        The old implementation deleted the host first.  A Docker failure then
        turned a transient daemon problem into data loss.  Stage and validate
        the copy before the original directory is moved aside, then roll back
        if the final rename fails.
        """
        host_path = Path(host_dir).expanduser().resolve(strict=False)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{host_path.name}.forge-stage-", dir=host_path.parent))
        backup: Path | None = None
        replaced = False
        try:
            copied = subprocess.run(
                ["docker", "cp", f"{self.container_name}:/workspace/.", str(stage)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if copied.returncode != 0:
                raise SandboxError(f"sandbox host sync failed: {copied.stderr[:300]}")

            for internal_name in (".git", self._HOST_IMPORT_MARKER, ".forge-internal"):
                internal = stage / internal_name
                if internal.is_dir():
                    shutil.rmtree(internal)
                elif internal.exists():
                    internal.unlink()

            if host_path.exists():
                backup = host_path.with_name(f".{host_path.name}.forge-backup-{uuid.uuid4().hex}")
                os.replace(host_path, backup)
            try:
                os.replace(stage, host_path)
                replaced = True
            except Exception:
                if backup is not None and backup.exists() and not host_path.exists():
                    os.replace(backup, host_path)
                raise
            if backup is not None:
                shutil.rmtree(backup)
        finally:
            if not replaced and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def export_snapshot(self, dest: str) -> SnapshotResult:
        """Copy the container's authoritative /workspace into an immutable stage.

        Reads from the container volume (the real workspace), never the host
        mirror, so acceptance judges what the agent actually produced. Runs only
        against a terminal container — the caller guarantees termination first."""
        dest_path = Path(dest).expanduser().resolve(strict=False)
        stage = Path(tempfile.mkdtemp(prefix="forge-snap-src-"))
        archive = stage.parent / f".{stage.name}.tar"
        try:
            command = ["docker", "exec", self.container_name, "tar", "-C", "/workspace"]
            for name in sorted(_SNAPSHOT_PRUNE):
                command.extend(["--exclude", f"./{name}", "--exclude", f"*/{name}"])
            command.extend(["-cf", "-", "."])
            with archive.open("wb") as output:
                copied = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, timeout=120)
            if copied.returncode != 0:
                detail = copied.stderr.decode("utf-8", "replace")[:300]
                raise SandboxError(f"snapshot export failed: {detail}")
            extracted = subprocess.run(
                ["tar", "-xf", str(archive), "-C", str(stage)],
                capture_output=True, text=True, timeout=120,
            )
            if extracted.returncode != 0:
                raise SandboxError(f"snapshot extraction failed: {extracted.stderr[:300]}")
            return _stage_dir_snapshot(stage, dest_path)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(stage, ignore_errors=True)


_WORKSPACE_CACHE: dict[str, Workspace] = {}


def get_workspace(project_id: str, host_path: str) -> Workspace:
    """Resolve the active Workspace for a project — the single entry point
    every tool should use instead of touching the filesystem/subprocess
    directly. Cached per project so the container is created once and reused
    (not recreated every tool call). Host mode is explicit; container mode
    fails closed rather than silently changing the trust boundary."""
    cached = _WORKSPACE_CACHE.get(project_id)
    if cached is not None:
        return cached

    if sandbox_mode() == "host":
        ws = HostWorkspace(host_path)
        _WORKSPACE_CACHE[project_id] = ws
        return ws

    if not docker_available():
        raise SandboxError(
            "container sandbox is required but Docker is unavailable; set FORGE_SANDBOX_MODE=host "
            "only after accepting host workspace risk"
        )
    try:
        ws = ContainerSandbox(project_id, host_path)
    except Exception as error:
        raise SandboxError(
            f"container sandbox initialization failed: {str(error)[:300]}; "
            "set FORGE_SANDBOX_MODE=host only after accepting host workspace risk"
        ) from error
    print(f"[sandbox] project {project_id[:12]} running in container "
          f"{ws.container_name} (non-root, cap-drop=ALL, no host filesystem access).")
    _WORKSPACE_CACHE[project_id] = ws
    return ws


def reset_workspace_cache() -> None:
    """Test-only: forget cached Workspace instances between test cases."""
    _WORKSPACE_CACHE.clear()
