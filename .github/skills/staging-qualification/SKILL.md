---
name: staging-qualification
description: Launch incremental Sweden Central staging with raw-span Sol qualification.
---

# Staging qualification

Read [contributor boundaries](../../../AGENTS.md),
[quality rules](../../../docs/QUALITY_BAR.md), and
[operations](../../../docs/OPERATIONS.md). Use the explicitly authorized committed
candidate, set `PYTHONPATH` to its `src`, and confirm module resolution.
Python owns deployment, invocation, raw telemetry collection, assessment and recovery:

```powershell
python -m agent_insights_quality run-staging
```

First use covers the reviewed inventory. Use `--full` only for an explicitly requested
complete inventory exercise. Normal runs select changed, missing or incomplete
targets; unchanged completed results retain their actual source, status and date.
Expectation/verifier-only changes reassess usable saved evidence, without fresh Agent traffic.

Python applies the recorded staging policy, including
[scoped single-root hygiene](../../../docs/QUALITY_BAR.md#scoped-single-root-hygiene).
Under v3, expected activation and additional independent Agent defects are separate;
same-root symptoms and handled operational behavior are not automatically extra defects.
Keep PASS, FAIL, INCOMPLETE and historical NOT_EVALUATED hygiene distinct. Do not perform
manual model assessments, invent a validation mode or resample a miss to obtain a pass.

Before recovery, inspect the existing process and checkpoints; repeat the same command
only to resume matching work. Preserve private checkpoints/logs/evidence and unknown
provider outcomes; never delete resources or invent run/generation IDs. Staging creates
no monitors, Agent Insights runs, Daily score, team report or promotion approval.
Report unresolved access/provider blockers while leaving unrelated completed work intact.

Same-source recovery retains the original run and date even across midnight. A completed full
run is not repeated automatically. Use `--full --new-run` only for an explicitly requested fresh
full exercise after the previous full run completed, never to discard interrupted work.

An explicitly authorized single-target fresh measurement uses
`run-staging --target finance-agent/issue-019 --new-run`, with one exact catalog key.
It runs all ten canonical attempts, not selected misses, and never adds other targets.
Repeat that command (or omit `--new-run`) to resume the same frozen measurement, even
after a final FAIL/INCOMPLETE; use its committed source. A separately authorized later
measurement requires `--new-run --after-run <actual-previous-targeted-run-id>`.
Unresolved remote work or an active writer blocks replacement. Preserve old artifacts;
do not claim full-inventory qualification from this one-target result. See the
[single-target recovery contract](../../../docs/OPERATIONS.md#explicit-single-target-staging).

For retained trace-context diagnostics, follow
[the read-only audit](../../../docs/OPERATIONS.md#caller-invocation-context).
Session precreation is a separate explicitly authorized
[staging trial](../../../docs/STAGING_PREPARATION_TRIAL.md), not a default warmup.
Use saved evidence for its audit; neither audit enables Daily, changes a judgment or
authorizes extra traffic. Staging and Daily retain separate execution boundaries.
