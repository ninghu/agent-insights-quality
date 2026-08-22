# Agent Insights Quality

`agent-insights-quality` is a public, reusable qualification framework for the Agent Insights engine.
The current `0.1.x` release includes five reviewed healthy synthetic agents, Foundry deployment and
endpoint-only traffic adapters, generic production infrastructure and orchestration boundaries,
deterministic scoring, Copilot judgment handoffs, quality-memory reconciliation, ADO synchronization,
reporting/email handoffs, and a fail-closed finalizer. The live daily entrypoint is implemented but
remains disabled by the reviewed readiness contract until operational qualification is complete.

The quality bar is intentionally strict. A day is `AT BAR` only after its complete reviewed daily
selection runs, healthy agents produce no insights, actual insight counts exactly match expected
root-cause counts, all structural checks pass, high-severity recall is 100%,
overall recall is at least 90%, precision is at least 95%, accepted insight attributes are all
correct, every run's observed count exactly matches the sum of its selected assignments' expected
finding counts, and duplicate, umbrella, and stale-version rates are zero. Extra findings are noise;
missing findings are misses. Incomplete or untrustworthy runs are `INCONCLUSIVE`, never passes.

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

Runtime coordinates are accepted only through protected environment variables. Daily deployment
requires `AIQ_AZURE_RESOURCE_GROUP`, `AIQ_FOUNDRY_ACCOUNT`,
`AIQ_APPLICATION_INSIGHTS_RESOURCE_ID`, and `AIQ_CONTAINER_REGISTRY_NAME`; it derives no resource
coordinate from repository content. Azure selection
supports either `AIQ_AZURE_SUBSCRIPTION_ID` or an exact `AIQ_AZURE_SUBSCRIPTION_NAME`. The runtime can
also discover exactly one project tagged `agentInsightsQualityQualification=true` and with the
configured `automationOwner`, avoiding persisted resource identifiers in scheduled Copilot sessions.
Use `python -m agent_insights_quality preflight --discover-project` to validate identity, AzureCloud,
the exact subscription, project/account shape, App Insights connection, Terra deployments, and Agent
Insights authorization without printing private values. The daily entrypoint must first deploy the
reviewed `qualification-project.bicep` module with the exact report date and catalog digest; runtime
selection fails closed and never creates a partially connected project.

The CLI exposes `materialize-execution-plan`, `run`, `resume`, `status`, and dry-run `cleanup`;
destructive cleanup requires `cleanup --execute`. `run` and `resume` default to the allowlisted
`agent_insights_quality.live_adapter`; setting
`AIQ_RUNTIME_ADAPTER=agent_insights_quality.live_adapter` is also supported. The custom-container
route additionally requires a protected digest-pinned `AIQ_TICKET_IMAGE_URI` for the exact reviewed
GHCR repository or owned Azure Container Registry repository. Receipts and CLI output contain opaque
hashes rather than private coordinates or URLs.

## Daily automation

`config/runtime-readiness.yaml` is the fail-closed authority. The healthy-agent contracts are
implemented, but `healthy_agents` remains false until a live run proves the required agent, model,
and tool spans through read-only Application Insights evidence. Therefore
`python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD>` returns an actionable
`INCONCLUSIVE` result without deployments, traffic, insights, ADO, memory transitions, cleanup, or
generated PR mutation. The required minimal finalizer still renders a sanitized report and
one-message email handoff so readiness failures cannot bypass finalization. The handoff preserves one
content digest and orders connected Copilot mail, authorized Graph, then verified local Outlook on
`hostId=local`; it stops after the first confirmed success and never uses a Logic App.

After a reviewed readiness change enables every component, `run-daily` writes or verifies the
immutable weekday plan before any Azure operation, deploys `qualification-project.bicep` with the
exact plan project/date/expiry/catalog hash and full project name as the connection suffix, and runs
or resumes the live adapter from `.aiq-runtime/`. A durable bounded 15-minute propagation gate after
Bicep allows the project managed identity and ACR pull authorization to converge before preflight;
terminal deployment `CodeError` remains an operational failure, never a quality result. Evidence completion writes a schema-validated,
public-safe `daily-status.json`; its ordered handoff uses the existing judgment, scoring, memory,
candidate-only ADO, reporting, email-receipt, generated-path, and cleanup commands. Use `--rerun N`
for `aiq-YYYYMMDD-rNN`; a finalized failed plan is immutable and requires a new rerun suffix.

Hosted-code and custom-container recovery, creation, and activation polling share a process-wide
serialization gate to avoid cross-hosted deployment contention. Prompt deployments may remain
parallel, and endpoint traffic remains parallel after deployment.
Prompt and hosted session calls retry only pre-response HTTP 408, 429, and 5xx failures. Missing
`Retry-After` uses conservative bounded exponential backoff; completed fixture receipts are
recovered on resume, nontransient 400s are not retried, and response bodies never enter public state.
Every generated traffic envelope retains the compatible healthy agent's reviewed domain request and
expected tools; scenario identity, provenance, correlation, and a bounded recipe marker are additive.
Zero-finding prompt traffic enforces the expected tool sequence and requires a grounded nonempty final
answer after tool output; fault-injection traffic remains relaxed only where the scenario requires it.

`config/ado-policy.yaml` is the reviewed public authority for ADO side effects. Candidate reporting is
enabled and automatic apply is disabled by default. `AIQ_ADO_AUTO_APPLY_ENABLED` can further disable
an approved apply policy, but it cannot enable apply when the file says false. Generated automation
cannot edit this or any other file under `config/`; enabling writes requires a normal human-reviewed
configuration change. Template reads, work-item reads, and WIQL duplicate searches remain read-only
planning operations.

After every mandatory component is implemented, validated, and human-reviewed, the scheduled Copilot
automation will follow `.github/skills/agent-insights-quality-daily/SKILL.md`. Qualification uses the
protected test-recipient variable; promotion to the protected production-recipient variable requires
a separate human-reviewed configuration change.
