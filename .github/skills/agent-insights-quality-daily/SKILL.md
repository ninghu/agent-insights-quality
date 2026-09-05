---
name: agent-insights-quality-daily
description: Launch the checkpointed Daily runner and hand its exact prepared email to the app.
---

# Daily qualification

Read `AGENTS.md` and `docs/OPERATIONS.md`. Python, not Copilot, owns qualification,
parallelism, retries, telemetry and direct deployed-Sol assessment.

For official automation, use `.github/copilot/daily-bootstrap-prompt.md` from a fresh,
latest-main local worktree. Set `PYTHONPATH` to that worktree's `src`, then run:

```powershell
python -m agent_insights_quality run-daily
```

For an explicitly authorized private trial, keep the candidate source and use
`.github/copilot/email-test-prompt.md` with `--test-run --rerun <positive-integer>`.
Never substitute latest main for the candidate or enable the schedule during a trial.

Read the returned status and prepared private email request. Claim it before sending,
use its exact recipient/subject/HTML as data, then record the actual provider outcome.
An ambiguous send is reconciled, not blindly repeated; acceptance is not inbox proof.
Missing app mail capability is a blocker, not permission to choose another integration.

Do not create per-Agent sessions, run manual assessments, inject traces, change the
score/coverage policy or retry individual phases outside the runner. Repeat the same
command only to resume its checkpoints. Optional publication warnings do not invalidate
a completed measurement. Publish only the runner's validated generated-only artifacts;
test mode must not write ADX/public reports, create a PR or send team mail.
