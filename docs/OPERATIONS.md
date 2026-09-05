# Operations

Use an authenticated local environment. Keep configuration and generated private artifacts under
`$HOME\.aiq-runtime\agent-insights-quality\`, never in the repository.

Set `PYTHONPATH` to the active source checkout before repository Python commands. Official automation
uses a fresh latest-main worktree; a private trial uses the candidate being evaluated.

## Entry points

```powershell
python -m agent_insights_quality validate
python -m agent_insights_quality generate-docs
python -m agent_insights_quality run-staging
python -m agent_insights_quality run-staging --full
python -m agent_insights_quality run-daily --test-run --rerun 1
python -m agent_insights_quality status --profile staging
python -m agent_insights_quality status --profile daily
```

`generate-docs` updates `AGENT_CATALOG.md` and `ISSUE_CATALOG.md`; it never rewrites traffic
or expected behavior. Runtime commands now use the replacement runner; there is no legacy
fallback. Production readiness still requires the candidate's deployed trial and TEST email.

## Recovery

Repeat the same command to resume matching work. Do not manually invent generation IDs, clear
state, delete Agent objects or resend completed traffic. A code/scenario change selects affected
work; unchanged completed results retain their original provenance.

Staging resumes the same source and selection mode across midnight, retaining its original run
date and completed calls. Repeating a completed full run is a no-op. Only an explicitly requested
fresh full exercise uses `run-staging --full --new-run`; it cannot replace an unfinished full run.

Preserve pending deployment/session/Insights records after an interrupted request. Unknown accepted
POSTs are not safe to repeat. Native Insights submission keys and their exact request bodies are
reused when supported. A definitively rejected submission gets a fresh key and recomputed lookback;
uncertain window coverage is excluded from Engine scoring, not counted as a missed issue.
A new email delivery test uses an explicit nonzero rerun identity; an
ambiguous previous send is reconciled, never sent again blindly.

Local `runner.log` and `events.jsonl` preserve starts, heartbeats, retries and outcomes across
restart. They work without ADX. Raw evidence is kept separately. Status reads do not take the
writer lock or perform live work.

## Interactive long-running work

During an interactive rollout, a nested app session can own the entire staging or private Daily
runner command while its parent monitors progress and coordinates fixes. Python still owns all
internal orchestration; do not create per-Agent orchestration sessions.

Monitor `QualityOperationsV1` by the exact `FrameworkRunId`, using local checkpoints as the
authoritative outcome and recovery state. An idle app session or an old heartbeat is not proof
that its runner completed. After an app restart, confirm whether the local process survived
before resuming the same command under normal runtime ownership; never bypass a lock or start
a second writer. Nested sessions do not guarantee process survival across app restarts.

Independent repair work can proceed in separate worktrees without modifying the running candidate.
Integrate repairs after the active run ends, then let incremental selection choose affected units.

## Environment boundaries

Staging uses `aiq-staging-swedencentral`; Daily uses `aiq-daily-swedencentral`. Both reuse Agent
objects and separate g30 telemetry. The canonical deployment registry is in the dedicated
Sweden private `deployment-registries` blob container. There is no legacy-region/storage fallback.

Staging never runs Agent Insights, scores Daily cards or sends a team report. It may emit safe
operational events. An email-only Daily test writes private evidence/previews/logs and sends only
to the configured private recipient: no public report, ADX writes or generated PR.

When blocked by access, a provider capability or an ambiguous outcome, keep checkpoints and surface
the specific decision needed. Continue unrelated safe work rather than hiding the blocker or
lowering the evidence bar.
