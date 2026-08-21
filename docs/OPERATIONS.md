# Operations

## Validate a change

Use Python 3.11 or newer:

```powershell
python -m pip install -e ".[dev]"
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m pytest
```

Generated automation branches use the `aiq-daily/` prefix. CI restricts those branches to the paths
in `config/automation-policy.yaml`. Source contracts, policies, schemas, prompts, skills, and
reporting configuration require a normal human-reviewed change.

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
