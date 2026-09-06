---
name: agent-insights-quality-daily
description: Launch the checkpointed Daily runner and hand its exact prepared email to the app.
---

# Daily qualification

Read [contributor boundaries](../../../AGENTS.md) and
[operations](../../../docs/OPERATIONS.md). Python, not Copilot, owns qualification,
parallelism, retries, telemetry, the frozen deployed assessor and report preparation.

Use the single `.github/copilot/daily-bootstrap-prompt.md`. The human supplies only
REPORT_MODE (`test` or `official`) and one literal TO_ADDRESS. Set `PYTHONPATH` to
the chosen worktree's `src`, then run:

```powershell
python -m agent_insights_quality run-daily --report-mode "$REPORT_MODE" --to-address "$TO_ADDRESS"
```

Stop before traffic for unfilled/invalid fields. Never infer mode from an address, accept
lists or use the team mailbox in TEST. Python automatically allocates a unique positive TEST
identity and freezes source/routing under runtime ownership. Repeat the same unified command
to resume unfinished/prepared/claimed/unknown work, including across midnight; source or
destination conflicts block. Only accepted/delivered/definitively rejected email permits a
later TEST invocation to allocate another identity. Do not add legacy identity flags.

For a new integrated automation launch use fresh latest main. For an explicitly authorized
manual candidate TEST keep the committed candidate. Recovery always uses the frozen source;
never fetch a newer main over unfinished work. Official is a weekday/date singleton: eligible
mail uses the exact frozen TO_ADDRESS, while failure notices always use the separately frozen
private fallback. Legacy official mail retains TEAM_RECIPIENT. Existing manually numbered
legacy commands remain available for recovery; the automatic pointer never adopts arbitrary
manual TEST runs. Template/default changes cannot redirect prepared mail.
Do not enable the schedule during a trial.

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
