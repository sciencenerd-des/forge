# Forge Rust operator CLI

The experimental Rust workspace contains the keyboard-first Forge operator
client. It talks to the Python control plane; it does not replace the PGE
engine or its sandbox.

```bash
cd experimental
cargo run -p forge-cli -- --help
```

The operator path (`health`, `run`, `approvals`, `provider`, and `tui`) needs
only the Rust binary and a running local Forge control plane. `session` is
optional and delegates the coding-agent loop to Pi over its JSONL RPC protocol.

See [`docs/RUST_CLI.md`](../docs/RUST_CLI.md) for configuration, key bindings,
and the pinned Pi compatibility version.
