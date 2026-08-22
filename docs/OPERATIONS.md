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

`config/runtime-readiness.yaml` records every mandatory runtime workstream. The live adapter and
daily orchestration boundary are implemented. Their readiness flags stay false until live telemetry
qualification proves the expected agent, model, and tool spans and required Azure permissions,
including `roleAssignments/write`, are demonstrated.
`check-runtime-readiness` and `run-daily` fail closed with an actionable `INCONCLUSIVE` result until
every component is implemented, tested, and enabled through a human-reviewed source change. A
readiness failure prohibits all operational phases but still requires the minimal report/email
finalizer and its one-message Copilot mail handoff. The readiness file is protected from generated
automation.

## Runtime commands

Supply private values only through the authorized runtime environment. Select Azure with exactly one
of `AIQ_AZURE_SUBSCRIPTION_ID` or `AIQ_AZURE_SUBSCRIPTION_NAME`. Explicit GitHub Actions execution can
also supply the resource group, Foundry account, project name, project endpoint, and Application
Insights resource ID. `run-daily` instead requires the protected resource group, Foundry account,
Application Insights resource ID, and `AIQ_CONTAINER_REGISTRY_NAME`; the immutable plan supplies the
daily project name and Azure resolves its endpoint after deployment. Standalone preflight/run
commands can still discover exactly one qualification project by its reviewed ownership tags.

Both modes resolve the Terra traffic and insights deployment names plus the reviewed model version
from protected `AIQ_TERRA_*` variables. Optional protected tenant and user-object IDs tighten identity
selection further; when omitted, preflight still requires an interactive Azure user and verifies that
identity's access to every selected resource.

The runtime Azure credential coalesces concurrent token requests into one in-process cache entry per
scope and refreshes with at least five minutes remaining. Azure CLI acquisition failure is a sanitized
transient runtime error; no command arguments or token values enter public diagnostics.

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

The daily entrypoint must deploy `qualification-project.bicep` with the exact report date and catalog
digest before invoking the runtime. Runtime selection never creates a project because a raw project
PUT cannot safely provision the required API-key connection. A missing or mismatched preprovisioned
project fails closed.

After a successful Bicep result, `run-daily` records a private `deployed` receipt and waits a bounded
15 minutes for the new project managed identity and ACR authorization to propagate. It then records
that gate as complete before live preflight. A process interruption resumes the pending propagation
gate without redeploying Bicep. Role-assignment list presence alone is not treated as data-plane
readiness.

When `AIQ_TICKET_IMAGE_URI` points to Azure Container Registry, preflight requires that exact
project-scoped managed-identity ContainerRegistry connection and AcrPull assignment. Connection
names use an optional deterministic per-project suffix because the service reserves names across the
account workspace; omit the suffix only for the existing base-project compatibility path. Runtime
reconciliation never reads, creates, or updates API-key credentials: a project missing the
preprovisioned connection fails closed with instructions to deploy the qualification-project Bicep
module instead of being reported ready.

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
    automationOwner='<automation-owner>' catalogVersion='<catalog-version>' `
    connectionNameSuffix='<exact-plan-project-name>'
```
Set `AIQ_MONITOR_OWNERSHIP_RECEIPT` to a protected private-state path. Monitor ownership is recorded
there as project-, agent-, monitor-, and model-scoped opaque hashes because the service monitor API
does not expose a metadata field.

```powershell
python -m agent_insights_quality preflight --discover-project
python -m agent_insights_quality materialize-execution-plan --plan <plan.json>
python -m agent_insights_quality run --plan <plan.json> --state <private-state.json> --dry-run
python -m agent_insights_quality run --plan <plan.json> --state <private-state.json>
python -m agent_insights_quality resume --plan <plan.json> --state <private-state.json>
python -m agent_insights_quality status --state <private-state.json>
python -m agent_insights_quality cleanup
python -m agent_insights_quality cleanup --execute
```

The scheduled boundary is:

```powershell
python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD>
python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD> --rerun 1
```

It writes or validates `plan.json` and `plan.md` before loading protected runtime coordinates or
deploying Bicep. The enforced order is plan, Bicep, propagation wait, exact live preflight, then
runtime execution/resume. Runtime receipts, deployment receipts, primary judgment packages, and email
requests stay under the private `--state-root` (default `.aiq-runtime`). The public
`daily-status.json` is written only after every selected scenario has a final evidence reference.
It is the GPT-5.6 Sol handoff for primary judgments, candidate-only blinded verification,
deterministic scoring/mapping, memory reconciliation, candidate-only ADO, canonical finalization,
one-message receipt import, a generated-only PR, and reviewed cleanup.

Before that handoff, every physical run insight is deterministically assigned to exactly one
scenario package and receives exactly one primary judgment target. Each package retains the complete
bounded run collection needed to judge duplicate, fragmentation, and umbrella relationships. A
scenario with no originally assigned card also retains its null no-insight target, even when it owns
false-positive run noise, so missing expected findings and healthy-control behavior remain explicit.
Projected bundles record that scenario-level null-target requirement explicitly; owned cards and
run-noise context remain disjoint.
Run-wide exact counts and insight accounting must agree across all bundles and cover every physical
card; incomplete or duplicated coverage makes the handoff inconclusive.

The explicit null-target contract uses the versioned `primary-v2` and
`blinded-verifier-v2` prompts. Historical v1 judgments remain schema-readable, but package identity
and prompt hashes prevent importing them into v2 packages. R19 has no imported judgments before this
first live judging handoff.

A published operational `INCONCLUSIVE` report is terminal for that immutable plan so rerunning cannot
change an already rendered email. Use the next `--rerun N` suffix. A process interruption before
public finalization resumes the same private receipt and recovers completed remote operations.
Rerun agents use `aiq-NNN-domain-YYYYMMDD-rNN-wNN`; the initial run retains
`aiq-NNN-domain-YYYYMMDD-wNN`. This keeps agent/version identity distinct across same-day projects
that share one Application Insights component.
Hosted-code and custom-container deployment recovery, creation, and activation polling are
process-wide serialized across agents. Prompt deployment may remain parallel; endpoint traffic
continues in parallel after deployment. A provisioning `CodeError` can be eventually consistent, so
the client polls the exact created version for a bounded 15-minute grace and requires consecutive
observations before failing. It never creates a duplicate version during stabilization. A persistent
`CodeError` is finalized as an operational failure and is never scored as Agent Insights quality.
Endpoint HTTP 408, 429, and 5xx responses are transient only when the failed request has no response
ID. The invocation client retries that exact prompt or session request up to three times, using
`Retry-After` when it is a bounded numeric value and otherwise waiting 60 then 120 seconds. If an
outer resume is still required, durable per-fixture receipts skip every confirmed success.
Nontransient 400s and failures carrying a response ID are not retried, and response bodies are not
stored in public runtime state.

Deployment create HTTP 408, 429, and 5xx responses are also typed transient failures with only safe
status and bounded `Retry-After` metadata. Every orchestrator retry re-runs immutable ownership/content
recovery before any create request, so a version created despite a lost response is reused rather than
duplicated. Other 4xx responses remain nontransient.
The scenario envelope preserves the selected healthy fixture's real domain input and expected tool
contract. Scenario ID, runtime provenance, correlation, and the bounded synthetic recipe marker are
added around that request; the generic recipe marker never replaces the domain request.
For every zero-finding prompt assignment, runtime validation requires the exact expected tool
sequence and a nonempty grounded final answer containing the reviewed tool result. Faulted
assignments may relax output/tool validation only so the injected behavior can reach Agent Insights.
Hosted healthy instructions map every finance, travel, and support command prefix to one exact tool
and require its result verbatim, preventing model-selected prerequisite or adjacent-tool detours.

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

`run` and `resume` use the built-in allowlisted `agent_insights_quality.live_adapter` by default. The
same exact module may be selected through `AIQ_RUNTIME_ADAPTER` or `--adapter`; arbitrary module
injection is rejected. Set protected `AIQ_TICKET_IMAGE_URI` to the reviewed GHCR image or exact owned
`<registry>.azurecr.io/agent-insights-quality-ticket` repository pinned by digest. Independent agents
run concurrently while versions of one agent remain sequential.
Symbolic plan windows remain immutable; endpoint traffic binds them to exact UTC half-open windows in
the runtime receipt, and both wave order and realized traffic non-overlap are checked. Agent Insights
uses a 3-2160 hour lookback covering elapsed time since the realized traffic start plus ingestion
margin. Sequential service analysis windows may overlap, but each successful window end must advance
beyond the prior checkpoint; changed revisions are then scoped to the exact agent version, correlated
operation IDs, trace timestamps, and publication bounds. Resume recovers the persisted lookback and
private idempotency receipts without replaying completed remote side effects.
An ordinary runtime or unexpected work failure stops only that agent's sequential work. Independent
agents drain to their own terminal evidence or failure checkpoint, and one bounded aggregate
`INCONCLUSIVE` failure records safe codes and opaque work references after maximum progress. The
four-hour orchestration deadline is checked before each new or retried step. Deployments and receipts
are retained on these failures, so resume skips every complete agent/version and retries only failed
or unstarted work. Only an explicit operator abort invokes cancellation and exact-owned-resource
cleanup, with a bounded worker drain and a final cleanup sweep. An explicitly aborted receipt is not
resumable because its deployment resources may have been removed; start an explicit rerun instead.
The runtime writes that durable `run_cancelled` marker before the first cleanup hook and patches any
cleanup failure codes afterward. Abort intent received while a resume receipt is loading is retained
but cannot trigger cleanup until the receipt and plan identity have been validated.
Each pre-run checkpoint captures revisions for every version on the exact monitor/agent. Unchanged
prior-version cards are therefore skipped before current-version validation; any prior-version card
whose revision changes during the run still fails strict stale provenance. Exact run Insight totals
are unbounded for scoring; evidence retains at most 100 detail samples and records `sampled_count` and
`details_truncated`. Cleanup is a dry run unless `--execute` is present and filters
exact framework purpose, owner, name, and expiration metadata.

For reviewed lifecycle sequences, evidence carries a bounded opaque set of exact prior-phase trace
IDs recovered from persisted scenario telemetry. A current-version insight linked to those proven
prior traces is a `cross_version_stale` quality failure (`NOT AT BAR`), including a prior-only link.
Any trace outside the current and exact planned prior sets remains unproven provenance and makes the
run `INCONCLUSIVE`; non-lifecycle evidence cannot declare prior traces. A prior service card is
optional because its absence is itself a lifecycle quality outcome. When present, its phase, run, and
digest must match a planned prior version or the evidence is untrustworthy.

Persisted trace evidence uses the immutable plan's symbolic project reference rather than a hash of
the resolved project resource ID. When loading successful legacy evidence, the live adapter validates
the stored schema and original bundle hash first, then may normalize references in memory only when
every trace has the same reference and it exactly matches the currently bound, validated project.
The raw artifact bytes and artifact reference are never changed or written back; mixed, foreign, or
unbound legacy provenance fails closed. Daily handoff first recovers the exact immutable plan's
project checkpoint, verifying its public checkpoint and hydrating the validated private binding
before any legacy evidence is loaded.

Foundry deployment requests use a bounded 300-second timeout. Hosted-version cancellation retries
HTTP 409 active-session conflicts for up to 15 minutes, never treats a conflict as deletion, and
preserves the cleanup failure code in append-only attempt-qualified diagnostics for a later resume.

The runtime executes every version selected by the reviewed plan; it does not stop scheduling at a
generic observed-insight threshold. Evidence compares each scenario's exact `finding_count` with all
trace-associated insights. Missing findings and extra noise are both recorded as `NOT_AT_BAR`.

Install the optional identity-backed Azure clients with `python -m pip install -e ".[azure]"` on live
runners that query Application Insights or use the Azure Blob artifact backend.

Scoring, Copilot judgment, quality memory, ADO synchronization, reporting/email handoffs, and the
daily orchestration boundary are implemented. `run-daily` catches readiness failure before all
operational work. Once enabled, it also finalizes any operational failure as a canonical
`INCONCLUSIVE` report plus one immutable unsent direct-email request. The readiness file remains
protected from generated automation.

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

Agent Insights prose and `no_fix` recommendations may omit `proposed_fix.changes`; the runtime
normalizes that omission to an empty list. The live service alias `prompt_change` normalizes exactly
to the internal `prompt_patch` evidence kind while preserving structured changes. Patch kinds require
an explicit list of change objects, and unknown kinds remain invalid.

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
with deterministic multipart ZIPs in the Foundry `code` field (including root `requirements.txt`
and Unix regular-file mode bits),
and the ticket version with a digest-pinned reviewed GHCR or owned
Azure Container Registry image.
After a hosted version reports active, deployment validation creates and removes one session bound
to that exact version. An active status that cannot create the exact-version session remains an
operational deployment failure.
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

Prompt tool use can produce one stored response operation per turn. Runtime receipts retain every
prompt response ID in order while keeping the final ID as the compatibility field. Initial and
intermediate turns require an exact `invoke_agent` operation; the final turn requires
`invoke_agent` plus `chat`. Every expanded operation remains bound to the originating scenario and is
included in its allowed trace/evidence set. Legacy private receipts fall back to their final response
ID only, so a new rerun is required to recover pre-final operations from an older run.

Hosted sessions may also emit multiple operation IDs. Correlation first anchors exactly one operation
to the fixture's response/invocation ID, then collects only complete operations carrying the same
exact `azure.ai.agentserver.session_id` or `microsoft.session.id`. Each operation is independently
validated and retains its expectation/scenario association; an operation cannot be reused across
fixtures.

Project-scoped telemetry may omit an upstream parent span. Correlation accepts exactly one local root
whose parent is empty or outside the selected span set, then requires every other span to be reachable
through in-set parents. Chat spans must use either the exact configured deployment name or the exact
canonical `gpt-5.6-terra-<AIQ_TERRA_MODEL_VERSION>` identity; prefixes and other versions are rejected.

Static source scanning enforces known direct-ingestion sinks while allowing legitimate read-only
Application Insights queries. Runtime egress and endpoint-only integration tests are the required
second layer when traffic implementations are added.
