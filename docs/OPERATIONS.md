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

Dedicated infrastructure creates the Foundry project's Application Insights connection with the
service-supported API-key shape. ARM resolves the existing Application Insights connection string
server-side and passes it directly to the connection resource; the secret is never a template
parameter or output and is not handled by the CLI. The project managed identity retains Monitoring
Reader on Application Insights, Cognitive Services OpenAI User on the Foundry account, and AcrPull on
the existing artifact registry. A project-level ContainerRegistry connection uses that managed
identity to pull private hosted-agent images. The connection is project-scoped, is not shared to all
projects, and passes the project principal ID and registry resource ID as the RegistryIdentity
credential shape required by the service. These values are ARM expressions evaluated server-side;
there are no connection outputs. The reviewed automation user receives only Storage Blob Data
Contributor on artifact storage and AcrPush on that registry. Supply its Microsoft Entra object ID at
deployment time; never commit it.

```powershell
az deployment sub create `
  --location westus2 `
  --template-file infra/main.bicep `
  --parameters resourceGroupName='<resource-group>' uniqueSuffix='<unique-suffix>' `
    terraModelVersion='<reviewed-model-version>' automationOwner='<automation-owner>' `
    automationPrincipalId='<automation-principal-object-id>'

az deployment group create `
  --resource-group '<resource-group>' `
  --template-file infra/modules/qualification-project.bicep `
  --parameters accountName='<foundry-account-name>' projectName='<qualification-project-name>' `
    applicationInsightsName='<application-insights-name>' registryName='<registry-name>' `
    reportDate='<YYYY-MM-DD>' expiresOn='<YYYY-MM-DD>' `
    automationOwner='<automation-owner>' catalogVersion='<catalog-version>'
```

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

## Daily issue assignment

`scenarios/catalog.yaml` remains the predefined, human-reviewed issue library; a scenario omitted
from one weekday plan remains active and returns on its reviewed cadence or deterministic rotation.
The default `rotating_daily` plan always includes all six zero-insight controls and nine single-root
P0 faults. The two-root `aiq-scn-062-umbrella-insight` collection-quality probe runs only on Monday,
Wednesday, and Friday because it is not a customer-safety probe. `config/selection-policy.yaml`
defines a Monday-Friday `9/10/9/10/9` rotating partition horizon that covers all 47 P1/P2 faults
exactly once. Expected root totals are `20/19/20/19/20`, so every agent stays at or below four.
The weekly capacity is 100 expected roots and the selection uses 98, leaving two review slots spare.

The catalog hash, policy hash, cycle number, and report date determine selection and assignment;
there is no mutable selection state. Friday advances to a new cycle on the next Monday; Saturday
and Sunday default plans fail closed. Priority, category coverage, and deterministic recency order
drive partitioning. Every plan records selected and omitted IDs, selection reasons, business-day
cycle identity/day, full-coverage horizon, and per-agent expected totals. The planner fails rather
than dropping an incompatible or over-cap selection.

The catalog cannot literally cover all eight fault categories every weekday while also selecting
each rotating scenario exactly once: the rotating library contains only three latency, four cost,
and four hallucination scenarios, and no P0 scenario in those categories. The deterministic
partition therefore maximizes coverage: tool and output appear all five days, latency three days,
and cost and hallucination four days; P0 supplies context, reliability, and safety every weekday.

Use `python -m agent_insights_quality plan --report-date YYYY-MM-DD` for normal human-reviewable
weekday qualification. A weekend date is rejected. `--full-catalog` is an explicit special-release mode, is labeled
non-human-daily, and is never used by scheduled automation. The expected cap is four roots per
agent across all versions in a daily project and four roots per run. There is no generic actual
insight cap: actual insights must equal the selected expected count exactly. Any extra insight is
noise and any missing insight is a miss, making a complete run `NOT AT BAR`.

Canonical reporting is scoped to the validated plan snapshot. `active_scenarios` equals the number
of selected assignments, and `scenario_results` must contain exactly one result for every selected
scenario with no unselected result. An incomplete selected scenario remains present with an
`inconclusive` result and makes the report `INCONCLUSIVE`; catalog scenarios omitted by rotation are
recorded in `plan.selection.omitted_scenario_ids` and do not block a conclusive daily verdict.
Explicit full-catalog plans select all 63 scenarios and therefore still require all 63 results.

`run` and `resume` require a reviewed adapter module through `AIQ_RUNTIME_ADAPTER`. Independent agents
run concurrently while versions of one agent remain sequential. Resume replays completed operations
with their idempotency keys and rejects checkpoint drift. Cleanup is a dry run unless `--execute` is
present and filters exact framework purpose, owner, name, and expiration metadata.

Install the optional identity-backed Azure clients with `python -m pip install -e ".[azure]"` on live
runners that query Application Insights or use the Azure Blob artifact backend.

Scoring, Copilot judgment, quality memory, ADO synchronization, and reporting/email handoffs are
implemented. `run-daily` catches readiness failure, persists the canonical `INCONCLUSIVE` report plus
an unsent direct-email handoff, and then returns nonzero. The readiness file is protected from
generated automation.

Generated automation branches use the `aiq-daily/` prefix. CI restricts those branches to the paths
in the **base branch's** `config/automation-policy.yaml`, using the base branch's installed validator.
The guard validates additions, changes, deletions, and both sides of renames. A generated PR cannot
authorize itself by modifying the allowlist, validator, reporting config, or readiness config. Source
contracts, policies, schemas, prompts, and skills require a normal human-reviewed change.

`config/ado-policy.yaml` enables candidate reporting and disables automatic ADO apply by default.
Template lookup, work-item reads, and WIQL duplicate search are allowed for planning. Create, patch,
reopen, and comment/evidence requests return a candidate-only result and issue no write HTTP request
unless `auto_apply_enabled` is changed to true in a normal human-reviewed configuration change.
`AIQ_ADO_AUTO_APPLY_ENABLED` may further disable that reviewed permission; it cannot turn a false
policy value on. Daily/generated automation must never edit this policy.

## Runtime handoff commands

Use `project-evidence`, `judge-package-export`, and `judge-package-import` for the primary Copilot
pass; use `verifier-export` and `verifier-import` for the independent blinded pass. `score` recomputes
aggregates from plans, evidence, and judgments. `memory-reconcile`, `ado-dry-run`, `ado-apply`,
`render-report`, `render-email`, `render-failure`, `email-receipt-import`, and `finalize` expose the
remaining deterministic handoffs. No command calls an external model or claims mail delivery without
an imported provider receipt.

`memory-reconcile` requires the validated daily plan and canonical report. Only a full-catalog,
evidence-complete report can advance issue state, and complete reports must be processed
chronologically.

`ado-apply` loads the reviewed ADO policy before private runtime configuration. With the default
policy, it succeeds only as an explicit candidate-only handoff and performs no ADO mutation.
`render-email` and `finalize` require `--runtime-link-context`; every private Agent Insights link is
matched exactly to that authorized subscription, resource group, account, project, and report plan.

The email request carries one immutable `content_digest` and an ordered, no-duplicate transport
policy. Automation tries connected Copilot mail first, Graph `/me/sendMail` only when `Mail.Send` is
confirmed, and local Outlook COM only on `hostId=local` for the verified authenticated-user test
mailbox. Outlook success requires Sent Items verification. Stop after the first confirmed success,
retain only opaque SHA receipt references, and never use a Logic App. A missing capability or Graph
403 is recorded as unavailable/unauthorized rather than treated as delivery.

## Reporting audience

`config/reporting.yaml` is the public-safe authority. Test mode uses the authenticated user's connected
Microsoft mailbox when an address-bearing automation environment is unavailable, or resolves only the
protected `AIQ_TEST_REPORT_RECIPIENT` variable in Actions/tests. Production mode resolves only
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
failure report, and creates an explicit unsent connected-mail request with bounded retry instructions.
Automation imports a provider receipt with ordered attempts, a matching content digest, and any
required mailbox/Sent Items checks before claiming delivery. Incomplete runs never advance clean
streaks or create, resolve, or reopen bugs.

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
