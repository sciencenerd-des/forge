# Community and launch plan

## Two weeks before launch

- Close every release gate in `RELEASE_CHECKLIST.md`.
- Recruit 10-20 private-alpha users across LM Studio, Ollama, vLLM, and one cloud endpoint.
- Publish three evidence-backed case studies.
- Record the 90-second demo and a 10-minute architecture walkthrough.
- Prepare repository topics, issue templates, Discussions categories, and good-first issues.

## Launch day

1. Publish GitHub release and demo.
2. Submit Show HN after the repository and CI are green.
3. Post to local-LLM and coding-agent communities with technical detail, not promotional repetition.
4. Publish LinkedIn/X posts pointing to the same reproducible evidence.
5. Respond to installation failures first; collect them into a public troubleshooting issue.

## First month cadence

- Weekly: benchmark trace or failure postmortem.
- Biweekly: contributor office hour or recorded build session.
- Monthly: release with changelog, metrics, security notes, and roadmap changes.

## Metrics

- Clean-machine quickstart completion rate
- First verified run completion rate
- Median time to first verified completion
- Human interventions per run
- Resume success after injected failure
- Seven-day active installations
- Issue response and close time
- External contributors and merged pull requests

## Feedback schema

Every bug report should request Forge commit, OS, execution boundary, model/provider, hardware, goal, run ID, relevant event/log excerpt, expected behavior, and actual behavior. Never request secrets or complete private repositories.
