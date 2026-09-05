---
name: staging-qualification
description: Launch incremental Sweden Central staging with raw-span Sol qualification.
---

# Staging qualification

Read `AGENTS.md` and `docs/QUALITY_BAR.md`. Use the committed candidate worktree and
set `PYTHONPATH` to its `src`. Python owns deployment, invocation, raw telemetry collection,
assessment and recovery:

```powershell
python -m agent_insights_quality run-staging
```

First use covers all five baselines and 36 issues. Use `--full` only for an explicitly
requested complete inventory exercise. Normal runs select changed, missing or incomplete
targets; unchanged completed results retain their actual source, status and date.
Expectation/verifier-only changes reassess usable saved evidence, without fresh Agent traffic.

Staging uses ten reviewed attempts per target, not deployed paired-v0 traffic.
Sol receives endpoint outputs and raw invocation-scoped spans, with gaps and citations.
Keep PASS, FAIL and INCOMPLETE distinct. Do not infer a new validation mode, resample a
behavioral miss or turn a tracing gap into an observed Agent failure.

Repeat the command to resume. Preserve private checkpoints/logs/evidence and unknown
provider outcomes; never delete resources or invent run/generation IDs. Staging creates
no monitors, Agent Insights runs, Daily score, team report or promotion approval.
Report unresolved access/provider blockers while leaving unrelated completed work intact.

Same-source recovery retains the original run and date even across midnight. A completed full
run is not repeated automatically. Use `--full --new-run` only for an explicitly requested fresh
full exercise after the previous full run completed, never to discard interrupted work.
