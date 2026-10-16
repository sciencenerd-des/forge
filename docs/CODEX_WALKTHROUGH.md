# A small verification lesson with Forge and Codex

**Audience:** builders who can run Python and want to review AI-generated code.
**Time:** 10–15 minutes. **Runtime:** Python 3.11+, Bash, and `tail`.
**Credentials, Docker, Postgres, model endpoint:** none for the first exercise.

## What you will learn

A test command can lie even when the test itself is correct. Bash normally
returns the exit status of the last stage of a pipeline. If a failed check is
piped into a successful output formatter, a caller may see success.

Forge's auditor includes `harden_pipefail`, which repairs this particular failure
shape. This walkthrough exercises the actual function from the source tree.

```bash
git clone https://github.com/sciencenerd-des/forge.git
cd forge
python3 examples/verification_demo.py
```

The demo prints four observations: a false pass, a preserved failure, a real
pass, and an unchanged result when hardening is applied twice. It raises an
error and exits nonzero if any expected observation is absent. All fixed demo
commands run in an automatically removed temporary directory.

## Inspect the evidence with Codex

Open this checkout in your Codex environment and use this prompt:

> Read AGENTS.md, examples/verification_demo.py, and harden_pipefail in
> engine/src/auditor.py. Explain why the first command exits zero. Trace the
> production function used by the demo. Propose one case that pipefail alone
> cannot fix. Do not change the acceptance criteria or call external services.

Then run the demo yourself. A plausible explanation is not a substitute for
the exit codes you observe.

For deeper practice, install the development dependencies and run the existing
regression suite for pipeline masking and fallback behavior:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest tests/regression/test_pipefail_hardening.py -q
```

Read the regression covering a real pytest failure falling through to a
successful `unittest` invocation that discovered no tests. `pipefail` alone
does not fix that outer fallback; selection of a runner must be separate from
the runner's pass/fail result. See [Lessons 1 and 13](LESSONS.md).

## What this proves and what remains separate

| Evidence | Supported conclusion | Outside its scope |
| --- | --- | --- |
| Demo negative control | This masked pipeline failure is preserved by the helper | All shell commands are safe |
| Demo positive control | A successful producer still passes | Generated work meets the user's full request |
| Demo idempotence check | The helper does not add another prefix on a second pass | Restart recovery or database durability |
| Regression suite | The covered failure cases behave as asserted | Live-model capability or end-to-end sandbox isolation |

The demo does not launch an autonomous run. Full runs require the services in
the [quickstart](../README.md#quickstart). Forge also contains an optional
Codex CLI auditor fallback; that is distinct from using Codex to develop and
review Forge. This tutorial makes no claim about which historical lines Codex
wrote, model benchmarks, user adoption, or provider endorsement.

## Turn it into a useful demonstration

Show the failing producer and the two exit codes on screen, inspect the
production helper, and close with the fallback counterexample. Retain the
commit SHA and commands alongside the recording so viewers can reproduce it.
Ask viewers to explain a negative control before inviting them to edit code.
