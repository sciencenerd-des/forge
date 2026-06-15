# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub's **Security advisories**
(Security → Report a vulnerability) rather than a public issue. We aim to
acknowledge reports within a few days.

## Scope and operational notes

Forge **executes model-proposed code and shell commands** to do its work. Treat any
run as untrusted code execution:

- Direct file tools are scoped to the project **workspace** (`FORGE_WORKSPACES`).
  Arbitrary model-proposed commands are disabled on the host by default because
  an interpreter or compiler can access anything available to the Forge OS user.
- Run the loop in a container or VM. `FORGE_ALLOW_HOST_EXECUTION=1` is an explicit
  unsafe opt-in for trusted local experiments, not a sandbox.
- **Never commit API keys.** Keys are read from the environment (`FORGE_LLM_API_KEY`
  or your provider's env var); `.env` is git-ignored.
- Every control-plane endpoint except `/health` requires the bearer token in
  `FORGE_CONTROL_TOKEN`. The bundled Compose stack binds its published ports to
  loopback; keep that boundary or add authenticated TLS ingress.
