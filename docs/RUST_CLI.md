# Rust operator CLI

The Rust `forge` binary is an experimental, keyboard-first client for the
local Forge control plane. It owns operator interactions—run lifecycle,
approvals, providers, and live events. The PGE engine remains Python.

## Install and run

```bash
cd experimental
cargo run -p forge-cli -- health
cargo run -p forge-cli -- tui
```

The client resolves:

- `FORGE_HOME`, falling back to `~/.forge`
- `FORGE_CONTROL_URL`, falling back to `http://127.0.0.1:8787`
- `FORGE_CONTROL_TOKEN`, otherwise `$FORGE_HOME/control-token`

Token files are repaired to mode `0600`, matching the Python server.

## Commands

```bash
forge health
forge config
forge run start --goal "Fix the test suite" --description "Investigate the regression"
forge run list --watch
forge run events RUN_ID --follow --json
forge approvals list --status pending
forge approvals decide APPROVAL_ID --approve --reason "Reviewed the action"
forge provider list
forge provider set executor --base-url http://127.0.0.1:1234/v1 --model local-model
forge a2a card
forge a2a send --project PROJECT_ID "Run the test suite and fix the failing parser"
forge a2a get TASK_ID
forge a2a cancel TASK_ID
forge tui
```

`forge run stop` takes a **project ID**; run events take a **run ID**. The
client keeps those identifiers as distinct Rust types. `forge run list
--watch` keeps polling through control-plane outages, alternating its
`source` field between `control-plane` and `offline-manifest`.

## TUI keys

| Key | Action |
| --- | --- |
| `j` / `k` or arrows | Select a run |
| `s` | Enter a goal and start a run |
| `x`, then `y` | Confirm stop for the selected project |
| `a` | Show pending approval state |
| `y` / `n`, then a reason and Enter | Approve or deny the pending approval |
| `?` | Toggle help |
| `q` | Quit operator mode |

### Session-mode keys

| Key | Action |
| --- | --- |
| `Enter` | Send the prompt (`Alt+Enter` inserts a newline) |
| `Esc` | Open the turn menu: `a` abort · `s` steer with the editor text · `f` follow-up |
| `Ctrl+R` | Session picker for this directory: `Enter` switch · `f` fork · `Esc` close |
| `Ctrl+M` / `Ctrl+T` / `Ctrl+K` | Cycle model / thinking level / compact context |
| `Ctrl+G` | Promote to a durable PGE goal |
| `Ctrl+Q` | Quit (a plain `q` is prompt text) |

The footer shows live session stats (model · thinking · context % · cost),
refreshed after every settled turn.

`Ctrl+G` runs a three-step review: type the goal, pick the target project
(projects with an active run are marked `● active run` and warned about),
then press `y` to explicitly create the durable PGE run. Escape cancels at
any step.

When the control plane is unavailable, the TUI shows a prominent **OFFLINE**
banner and displays only the read-only launcher manifest. It never merges that
fallback with live state.

## Pi sessions

The pinned compatibility version is `@earendil-works/pi-coding-agent@0.80.2`.

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.80.2
forge session --no-session
forge session --provider openai --model openai/gpt-4.1
```

Pi supplies its own agent loop and built-in `read`, `write`, `edit`, `bash`,
`grep`, `find`, and `ls` tools. Forge speaks Pi's documented strict-LF JSONL
RPC protocol and does not reimplement those tools.

## Packaging

The deliverable is one Rust binary, `forge`, built with rustls and without an
OpenSSL dependency. It uses ordinary dynamic linking on macOS/glibc targets;
it is not a fully static binary. Session mode additionally needs the optional,
external Pi executable above. No other executable ships.

## Measured local performance

Measured on the development machine on 2026-07-12 after a release build:

```text
hyperfine --warmup 3 --runs 20 './target/release/forge --help'
mean: 1.9 ms ± 0.9 ms (range 1.3–4.8 ms)
binary size: 3.9 MB
```

`hyperfine` warned that the command is faster than its shell-calibration
threshold, so treat this as a local cold-start indicator rather than a portable
benchmark claim. The result is below the 30 ms development target.
