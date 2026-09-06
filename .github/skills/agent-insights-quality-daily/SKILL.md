---
name: agent-insights-quality-daily
description: Launch the checkpointed Daily runner and hand its exact prepared email to the app.
---

# Daily qualification

Read [contributor boundaries](../../../AGENTS.md) and
[operations](../../../docs/OPERATIONS.md). Python, not Copilot, owns qualification,
parallelism, retries, telemetry, the frozen deployed assessor and report preparation.

For official automation, use `.github/copilot/daily-bootstrap-prompt.md` from a fresh,
latest-main local worktree. Set `PYTHONPATH` to that worktree's `src`, then run:

```powershell
python -m agent_insights_quality run-daily
```

For an explicitly authorized NEW private measurement, keep the committed candidate and use
`.github/copilot/email-test-prompt.md` with
`--test-run --rerun <new-positive-integer> --fresh-traffic`.
For recovery, first inspect the existing run/process and resume its original identity,
source and frozen intent; do not choose a new rerun or repeat completed traffic.
Never substitute latest main for the candidate or enable the schedule during a trial.

Read the returned status and prepared private email request. Claim it before sending,
use its exact recipient/subject/HTML as data, then record the actual provider outcome.
An ambiguous send is reconciled, not blindly repeated; acceptance is not inbox proof.
Missing app mail capability is a blocker, not permission to choose another integration.

Do not create per-Agent sessions, assess evidence, inject traces, change scoring or retry
individual phases outside the runner. Scoring and coverage belong to
[quality rules](../../../docs/QUALITY_BAR.md), not this automation prompt.

Python owns private report publication and approved access links. Follow
[publication-only recovery](../../../docs/OPERATIONS.md#automatic-private-report-publication)
for archive/access problems, never another measurement or email send. The app must not
upload, mint links, create report branches/PRs, or rewrite prepared content. An expired
unclaimed email is blocked; an access refresh preview does not authorize a replacement send.
Optional publication warnings do not invalidate a completed measurement.
TEST must not write ADX/public reports or official latest, create a PR or send team mail.
