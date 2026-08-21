# Agent Insights Quality

`agent-insights-quality` is a public, reusable qualification framework for the Agent Insights engine.
The current `0.1.x` release includes five reviewed healthy synthetic agents, Foundry deployment and
endpoint-only traffic adapters, generic production infrastructure and orchestration boundaries,
deterministic scoring, Copilot judgment handoffs, quality-memory reconciliation, ADO synchronization,
reporting/email handoffs, and a fail-closed finalizer. Live qualification remains disabled, so this
is not yet a live daily automation runtime.

The quality bar is intentionally strict. A day is `AT BAR` only after its complete reviewed daily
selection runs, healthy agents produce no insights, actual insight counts exactly match expected
root-cause counts, all structural checks pass, high-severity recall is 100%,
overall recall is at least 90%, precision is at least 95%, accepted insight attributes are all
correct, and duplicate, umbrella, and stale-version rates are zero. Incomplete or untrustworthy
runs are `INCONCLUSIVE`, never passes.

## Local development

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m agent_insights_quality validate
python -m pytest
```

Authoritative inputs live in `agents/**/manifest.yaml`, `scenarios/catalog.yaml`, `config/`, and
`schemas/`. After changing an agent, scenario, or quality-memory contract, run:

```powershell
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
```

Use the repository skills under `.github/skills/` to onboard an agent or scenario and to replay a
run safely. Generated documentation must never be edited by hand.

`scenarios/catalog.yaml` is the predefined reviewed issue library. The default weekday planner runs
all six healthy controls and nine single-root P0 faults, plus the two-root umbrella P0 collection
probe on Monday/Wednesday/Friday and one deterministic partition of the 47 P1/P2 faults. One
Monday-Friday cycle covers every rotating fault exactly once; weekends are not scheduled. Use
`--full-catalog` only for explicit release qualification; that mode is marked non-human-daily and
does not claim the per-agent expected-review cap.

## Security and privacy

Only generated synthetic data and public-safe configuration are permitted. Do not commit credentials,
tokens, Azure subscription or tenant identifiers, internal endpoints, raw traces, private production
prompt payloads, private ADO content, or real customer, health, or financial data. Credentials and
private resource identifiers must be supplied at runtime. Traffic must invoke deployed agent
endpoints; Application Insights is read-only evidence storage, and direct telemetry injection is
forbidden.

## Production runtime

Runtime coordinates are accepted only through protected environment variables. Azure selection
supports either `AIQ_AZURE_SUBSCRIPTION_ID` or an exact `AIQ_AZURE_SUBSCRIPTION_NAME`. The runtime can
also discover exactly one project tagged `agentInsightsQualityQualification=true` and with the
configured `automationOwner`, avoiding persisted resource identifiers in scheduled Copilot sessions.
Use `python -m agent_insights_quality preflight --discover-project` to validate identity, AzureCloud,
the exact subscription, project/account shape, App Insights connection, Terra deployments, and Agent
Insights authorization without printing private values.

The generic CLI exposes `run`, `resume`, `status`, and dry-run `cleanup`; destructive cleanup requires
`cleanup --execute`. Production `run` and `resume` load a reviewed adapter named by
`AIQ_RUNTIME_ADAPTER`, and fail closed when the deployment/traffic workstream is not installed.
Receipts and CLI output contain opaque hashes rather than private coordinates or URLs.

## Daily automation

`config/runtime-readiness.yaml` is the fail-closed authority. The healthy-agent contracts are
implemented, but `healthy_agents` remains false until a live run proves the required agent, model,
and tool spans through read-only Application Insights evidence. Therefore
`python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD>` returns an actionable
`INCONCLUSIVE` result without deployments, traffic, insights, ADO, memory transitions, cleanup, or
generated PR mutation. The required minimal finalizer still renders a sanitized report and
one-message email handoff for Copilot to send through the authenticated user's mailbox.

After every mandatory component is implemented, validated, and human-reviewed, the scheduled Copilot
automation will follow `.github/skills/agent-insights-quality-daily/SKILL.md`. Qualification uses the
protected test-recipient variable; promotion to the protected production-recipient variable requires
a separate human-reviewed configuration change.
