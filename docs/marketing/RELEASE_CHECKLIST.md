# OSS release checklist

## Security

- [ ] Model-proposed host commands denied by default and regression-tested.
- [ ] Every non-health API route requires authentication.
- [ ] Docker-published ports bind to loopback by default.
- [ ] No secrets appear in CLI config, screenshots, fixtures, or logs.
- [ ] SECURITY.md accurately states isolation and support boundaries.

## Product

- [ ] Clean-machine Docker quickstart reaches a verified completed run.
- [ ] Console loads runs, projects, approvals, and config through authenticated proxy.
- [ ] Stop confirmation, approval rationale, event filtering, and error states work.
- [ ] Empty, loading, failed, blocked, running, and completed states are understandable.

## Quality

- [ ] Python tests pass on Python 3.11 and 3.12.
- [ ] Ruff check passes.
- [ ] Frontend lint and production build pass.
- [ ] Rust tests pass.
- [ ] Desktop and mobile browser smoke tests pass.
- [ ] Keyboard-only workflow and visible focus pass.

## Launch

- [ ] Version, changelog, tag, and GitHub release agree.
- [ ] Demo and three case studies are published.
- [ ] Landing page and social card links resolve.
- [ ] Launch posts use evidence-backed claims.
- [ ] Maintainer response schedule is reserved for the first 72 hours.
