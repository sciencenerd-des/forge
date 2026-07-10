# Sandbox Boundary Remediation Source of Truth

This document is the implementation contract for the sandbox-boundary repair.
It takes precedence over convenience fallbacks, legacy comments, and feature
claims elsewhere in the repository until every acceptance check below passes.

## Meta prompt for this change

> Treat every model-generated command and path as untrusted. Never run an
> untrusted command on the Forge host. Never allow a tool path to escape its
> configured workspace. Never replace a host workspace until a complete staged
> copy has succeeded and can be rolled back. Prefer an explicit, actionable
> failure over an insecure fallback. Keep behavior deterministic, test the
> negative paths, and update operator documentation whenever a safety contract
> changes.

## Invariants

1. `ContainerSandbox` is the default and required mode. Host mode is an
   explicit operator opt-in only.
2. In either workspace implementation, tool paths must be relative to the
   workspace and must not escape through absolute paths, `..`, or symlinks.
3. Contract strength and mutation probes may execute generated test commands
   only inside a container sandbox. If that sandbox is unavailable, the probe
   must report that it was not run; it must never execute on the host.
4. Existing host workspaces must be copied to a new sandbox volume exactly once
   before the container is used. A failed copy leaves the host untouched.
5. Syncing container results back to the host uses a staged copy, verifies the
   Docker copy result, and rolls back on replacement failure.
6. Safety failures must be visible to the operator and block the affected
   completion path. They must not silently degrade into host execution.
7. The Compose services must not receive the host Docker socket: Docker-daemon
   access is host-root equivalent and bypasses the sandbox boundary.

## Anomalies to eliminate

- `HostWorkspace` used through `ToolContext.sandbox` bypassed the existing
  path-boundary validator.
- Contract probes used `/bin/bash -lc` on the host despite the container
  sandbox being enabled.
- A new volume started empty and `sync_to_host()` deleted the host workspace
  before verifying `docker cp` succeeded.
- Documentation claimed automatic host fallback, which conflicted with the
  secure-by-default boundary above.
- A Compose Docker-socket mount would have given an LLM-driven process
  unrestricted authority over the host daemon.

## Required tests

- Reject absolute paths, traversal, and symlink escapes in host and container
  workspace tool calls.
- Prove contract probes use a container runner and never invoke host
  `subprocess` for generated commands.
- Prove an existing host workspace is imported once before sandbox use.
- Prove failed migration and failed sync preserve the original host workspace.
- Prove a successful sync replaces the mirror atomically and excludes internal
  sandbox metadata.

## Documentation requirements

- README and `.env.example` must state that container mode fails closed and
  host mode requires explicit `FORGE_SANDBOX_MODE=host` plus the existing host
  execution acknowledgement for shell commands.
- Sandbox documentation must describe one-time workspace import and the
  transactional sync behavior.
- `docs/CONVERGENCE.md` must not claim a security property that the live graph
  does not enforce.
