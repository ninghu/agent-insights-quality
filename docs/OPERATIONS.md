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

`config/runtime-readiness.yaml` records every mandatory runtime workstream. The healthy-agent
deployment and traffic contracts plus generic production infrastructure and orchestration boundaries
are implemented. Their readiness flags stay false until live telemetry qualification proves the
expected agent, model, and tool spans, required Azure permissions including `roleAssignments/write`
are demonstrated, and a reviewed adapter binds these boundaries without changing their contracts.
`check-runtime-readiness` and `run-daily` fail closed with an actionable `INCONCLUSIVE` result until
every component is implemented, tested, and enabled through a human-reviewed source change. A
readiness failure prohibits all operational phases but still requires the minimal report/email
finalizer and its one-message Copilot mail handoff. The readiness file is protected from generated
automation.

## Runtime commands

Supply private values only through the authorized runtime environment. Select Azure with exactly one
of `AIQ_AZURE_SUBSCRIPTION_ID` or `AIQ_AZURE_SUBSCRIPTION_NAME`. Explicit GitHub Actions execution can
also supply the resource group, Foundry account, project name, project endpoint, and Application
Insights resource ID. Scheduled Copilot execution can omit those coordinates and discover exactly one qualification
project by its reviewed ownership tags.

Both modes resolve the Terra traffic and insights deployment names plus the reviewed model version
from protected `AIQ_TERRA_*` variables. Optional protected tenant and user-object IDs tighten identity
selection further; when omitted, preflight still requires an interactive Azure user and verifies that
identity's access to every selected resource.

Set `AIQ_MONITOR_OWNERSHIP_RECEIPT` to a protected private-state path. Monitor ownership is recorded
there as project-, agent-, monitor-, and model-scoped opaque hashes because the service monitor API
does not expose a metadata field.

```powershell
python -m agent_insights_quality preflight --discover-project
python -m agent_insights_quality run --plan <plan.json> --state <private-state.json> --dry-run
python -m agent_insights_quality run --plan <plan.json> --state <private-state.json>
python -m agent_insights_quality resume --plan <plan.json> --state <private-state.json>
python -m agent_insights_quality status --state <private-state.json>
python -m agent_insights_quality cleanup
python -m agent_insights_quality cleanup --execute
```

`run` and `resume` require a reviewed adapter module through `AIQ_RUNTIME_ADAPTER`. Independent agents
run concurrently while versions of one agent remain sequential. Resume replays completed operations
with their idempotency keys and rejects checkpoint drift. Cleanup is a dry run unless `--execute` is
present and filters exact framework purpose, owner, name, and expiration metadata.

Install the optional identity-backed Azure clients with `python -m pip install -e ".[azure]"` on live
runners that query Application Insights or use the Azure Blob artifact backend.

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
runtime. Never commit credentials, internal identifiers, raw traces, private production prompt
payloads, private work-item content, or real customer data. Sanitized reports may contain public-safe
hashes, counts, verdicts, and links only when the links themselves are approved for publication.

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

## Healthy agent deployment and traffic

The active definitions under `agents/` are deterministic and synthetic. Runtime code supplies a
short-lived `https://ai.azure.com/.default` token through a token provider and a private Foundry
project endpoint. `FoundryDeploymentClient` creates prompt versions with JSON, hosted-code versions
with deterministic multipart ZIPs, and the ticket version with a digest-pinned public GHCR image.
Every hosted version is polled to `active`, and cleanup deletes only the exact version whose owner,
run ID, and artifact digest match its receipt.

`FoundryInvocationClient` calls only the deployed Foundry endpoint. Prompt calls bind an exact
`agent_reference`; hosted calls create `/agents/{name}/endpoint/sessions` with a `version_ref`, pass
the returned `agent_session_id` to Responses, and delete that exact endpoint session afterward.
Receipts retain protocol response/invocation/session IDs plus transport request IDs for later
read-only correlation. None of these IDs are trace IDs.

Live qualification accepts only the exact active deployment and invocation receipts for one run and
a timezone-aware UTC window no longer than one hour. Read-only Application Insights evidence must
fall inside that half-open window, use valid 32-hex `operation_Id` and 16-hex span IDs, match every
response/session/version receipt, and form one rooted agent span tree with model and exact tool
name/argument/result spans for every prompt and hosted fixture. Stale, unrelated, duplicate, or
malformed evidence fails closed and cannot enable readiness.

The ticket image is published only for a push of the exact trusted `main` SHA, when the repository
variable `AIQ_GHCR_PUBLISH_ENABLED` is `true`, through the `ghcr-publish` environment. Before
enabling that variable, a repository administrator must protect the environment with required
reviewers and restrict it to `main`; the missing variable otherwise keeps publication disabled.
Pull requests and manual workflow runs build without pushing. Before deployment, configure the GHCR
package for anonymous pull access and pass the resulting
`ghcr.io/ninghu/agent-insights-quality-ticket@sha256:<digest>` reference at runtime. No Azure
endpoint, credential, or registry secret belongs in the image or repository.

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
