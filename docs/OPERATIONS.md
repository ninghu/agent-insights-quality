# Operations

## Validate a change

Use Python 3.11 or newer:

```powershell
python -m pip install -e ".[dev]"
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m pytest
```

## Runtime readiness

This release is contract scaffolding. `config/runtime-readiness.yaml` records every mandatory runtime
workstream, and all are initially false. `check-runtime-readiness` and `run-daily` fail closed with an
actionable `INCONCLUSIVE` result until every component is implemented, tested, and enabled through a
human-reviewed source change. A readiness failure prohibits all operational phases but still requires
the minimal report/email finalizer and its one-message Copilot mail handoff. The readiness file is
protected from generated automation.

Generated automation branches use the `aiq-daily/` prefix. CI restricts those branches to the paths
in the **base branch's** `config/automation-policy.yaml`, using the base branch's installed validator.
The guard validates additions, changes, deletions, and both sides of renames. A generated PR cannot
authorize itself by modifying the allowlist, validator, reporting config, or readiness config. Source
contracts, policies, schemas, prompts, and skills require a normal human-reviewed change.

## Reporting audience

`config/reporting.yaml` is the public-safe authority. Test mode resolves only the protected
`AIQ_TEST_REPORT_RECIPIENT` automation variable. Production mode resolves only
`AIQ_PRODUCTION_REPORT_RECIPIENT`. Both values must use the configured allowed domain. Promotion is
an explicit human-reviewed mode change; daily automation cannot modify configuration or promote
itself.

## Public-data boundary

This public repository contains synthetic data and public-safe contracts only. Supply tenant,
subscription, resource, endpoint, ADO, and mail capability details through the authorized private
runtime. Never commit credentials, internal identifiers, raw traces, complete prompt payloads,
private work-item content, or real customer data. Sanitized reports may contain public-safe hashes,
counts, verdicts, and links only when the links themselves are approved for publication.

## Failure behavior

Any unavailable identity, service, quota, trace set, judge, consistency check, or delivery
prerequisite makes the run `INCONCLUSIVE`. The finalizer preserves sanitized diagnostics, renders the
failure report, retries direct email with bounded backoff, and surfaces delivery failure. Incomplete
runs never advance clean streaks or create, resolve, or reopen bugs.

When readiness itself fails, the finalizer additionally prohibits Azure deployments, agent traffic,
Agent Insights access, ADO access, memory transitions, resource cleanup, and generated PR mutation.
It renders a pending handoff rather than claiming delivery; Copilot sends exactly one logical message
through connected Microsoft mail and records a sanitized receipt reference or failure result.

Cleanup resolves exact private runtime resource IDs and deletes only framework-tagged resources past
their retention date. It never guesses names or deletes unrelated resources.

## Runtime link contract

Agent Insights links are rendered at runtime from the private subscription, resource group, account,
and project values:

```text
https://ai.azure.com/nextgen/r/{sub},{rg},,{account},{project}/build/agents/{urlencodedAgent}/insights
```

When the standalone-tab flight is off, use the fallback suffix `/monitor/insights`. Trace links use
`/build/agents/{urlencodedAgent}/traces/{operation_Id}`. There are no supported monitor, run, or
individual-insight ID deep links; insight selection is router state only.

Endpoint invocation and response IDs are not trace IDs. Correlate them through read-only Application
Insights data to the trace `operation_Id` before creating a trace link. Runtime links may appear in
direct email and private ADO actions but must never be persisted in this public repository; committed
artifacts use opaque SHA-256 references.

Static source scanning enforces known direct-ingestion sinks while allowing legitimate read-only
Application Insights queries. Runtime egress and endpoint-only integration tests are the required
second layer when traffic implementations are added.
