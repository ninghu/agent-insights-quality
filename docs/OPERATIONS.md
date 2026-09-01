# Operations

## Environment model

`daily` and `staging` are isolated Foundry accounts and profiles. Never point one profile at the
other profile's account, Project, Application Insights resource, deployment registry, monitor, or
private artifact root.

| Profile | Project | Purpose |
| --- | --- | --- |
| `daily` | `aiq-daily-swedencentral` | Weekday qualification |
| `staging` | `aiq-staging-swedencentral` | Durable human-reviewed validation gate |

The old West US 2 environment and its final historical staging run remain untouched until a later
review explicitly authorizes retirement. Neither Sweden profile may discover or mutate legacy
resources or lifecycle state.

## Test Agent Validation

Test Agent Validation reuses the durable `aiq-staging-swedencentral` Account and identically named
Project, exact GPT-5.4 mini deployment, shared ACR/storage, and read-only Sweden `g30` telemetry. It
reconciles 41 stable baseline/issue Agent names to exact server-assigned provider versions and content
digests. It creates no monitor and does not run Agent Insights, Sol assessment, score/report
generation, ADX publication, email, or Daily.

The executable authority is each `traffic.json` `validation_rules` contract. Its automatic digest
includes complete setup/probe request bodies, ordered conversation grouping, parameters, fixtures,
semantic/trace/identity assertions, reviewed mode, `n`, `k`, runtime kind, framework, and model
contract. Baselines require five mechanically complete attempts; deterministic issue packages
require five issue and five paired-v0 attempts; model-mediated packages require seven of each.
Expected issue observations remain private-review context rather than a local verdict.
`minimum_traces` keeps its Daily meaning and is never used as a validation attempt threshold.

Before any topology mutation, acquire the Sweden-environment OS file lock and record measured RPM/TPM,
25-percent and absolute headroom, the complete endpoint envelope, inner model fan-out, and bounded
concurrency. Provision at most eight, query telemetry at most four, and allow one scenario
attempt per runtime. The shared bucket charges each request's input plus maximum output budget,
multiplied by reviewed inner fan-out, and a provider `Retry-After` pauses every consumer. Every
issue and paired-v0 attempt gets a globally unique execution, conversation, session, response, and
operation identity while receiving the same matrix. Evidence binds the exact deployed Agent/version and derives identity from correlated telemetry.
Semantic and trace assertions remain reviewed context for later private Copilot review; local
validation records only mechanical execution and evidence completeness and produces no issue verdict.

The local lifecycle is:

```text
LOCKED -> PREFLIGHT -> CREATING -> VALIDATING
  -> FINAL_CHECKS -> CLEANING -> CLEAN
```

The runner holds
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/validation.lock` for the whole process.
Every mutation atomically replaces the local active journal and writes a required content-addressed,
append-only history event. Process exit releases the lock. Before traffic, an unchanged commit may
resume the same cycle, durable Project, topology, exact source-digest Support images, and confirmed-ready
authorities; it retries only unresolved authorities and allows at most three recovered versions per
Agent. Phase 1 deploys only `weather-agent/v0` and `finance-agent/v0` and runs both official baseline
matrices concurrently. Both must pass before Phase 2 deploys the remaining 39 and runs five independent Agent lanes with
versions sequential inside each lane. A recoverable issue execution/evidence failure supersedes only
that provider version: the runner keeps the stable Agent name, reconciles a new server-assigned exact
version for the same content digest, and accepts only replacement evidence from its exact operation and
time window after bounded hydration/stability waiting. Completed authorities remain accepted
and are never rerun; pre-traffic source/contract failures that rebuilding cannot change and Phase 1
baselines are not eligible. Agent-local failures do not cancel other lanes; shared runtime failures do. Commit,
identity, substrate, topology, TTL, retry exhaustion, or post-traffic drift forces exact run-scoped
cleanup. Cleanup remains allowed after the fixed 72-hour execution TTL. The authenticated Azure CLI
user is resolved explicitly, recorded as a private hash, and checked on every SDK token acquisition.

Support wheelhouse artifacts and ACR build tags are reused only under exact requirements/source
digests. Cycle tags remain cycle-owned and are removed during cleanup. Public-safe per-stage timings
are persisted after each completed stage under the private validation runtime root.

Cleanup records intent first and deletes responses, conversations/sessions, cycle tags, and unshared
manifests. The durable Project, its connections/RBAC, and reconciled Agent/version topology are
explicitly retained. Final proof requires no run-scoped nonce-owned resources, sessions/responses,
cycle tags, or incomplete reviewed cascades. Ambiguity enters `CLEANUP_BLOCKED` only in the Sweden
environment namespace and never mutates the legacy lifecycle pointer.

Run local validation with no identity, path, or SHA arguments:

```powershell
python -m agent_insights_quality run-test-agent-validation
```

The command discovers the repository, open PR, exact clean commit, and runtime paths. Any commit
change forces cleanup and a fresh full cycle. A successful run writes local evidence and CLEAN only.
After the user explicitly approves that exact result, run:

```powershell
python -m agent_insights_quality approve-test-agent-validation
```

The approval command re-reads the exact PR head and local content hashes, then create-once writes one
minimal immutable approved record. It has no workflow, App, check, tree, topology, quota, or local-path
fields. GitHub runs ordinary mechanical CI only and merge remains manual.

### External Daily approved-record cutover

The separate Daily owner must dry-run a **new-only** approved-record consumer; no process may read
both legacy promotion receipts and approved validation records. Under the Daily quiescence lock,
prove no run/provision/publication is active, pause the scheduler, switch the active source atomically,
and remain new-only on failure.

Daily checks out the record's exact commit and recomputes catalogs, Agent source, and the validation
contract before provisioning. Then perform only read-only readiness and registry reconciliation; do
not send smoke traffic. For this migration only, after readiness succeeds, the Daily owner may run
the explicitly requested isolated `--test-run --rerun N` email-only Daily Test. It is external,
non-gating, and writes no ADX, official report/trend, or pull request.

The old West US 2 resources remain unchanged after cutover; retirement requires a separate reviewed
authorization. That old environment is never a staging fallback.

Both use 90-day telemetry and artifact retention. The shared ADX quality-history database retains
sanitized daily results and explanations for 730 days and keeps 90 days in hot cache.

The reviewed fixed telemetry resource set is stored in `config/automation.yaml`; it is not part of
the Agent deployment catalog hash. Daily and staging each keep one App Insights and Log Analytics
pair. Routine runs, reruns, and Agent changes reuse them.

## One-time deployment

Infrastructure values come only from protected runtime configuration:

The currently signed-in Azure user receives Foundry Project Manager, Monitoring Reader, artifact
storage, ACR push, and ADX Database Viewer and Ingestor access for one-time reviewed provisioning.
Daily execution does not build or push images.

```powershell
python -m agent_insights_quality deploy-infrastructure
python -m agent_insights_quality deploy-analytics
```

The infrastructure command creates only the two Sweden accounts, identically named Projects,
Project-scoped `g30` telemetry pairs, and exact reviewed model deployments in the existing resource
group. It references the existing shared ACR and Blob storage and does not create, update, or delete
existing containers or data. It creates only the environment-namespaced
`test-agent-validation-approved-records-swedencentral-g30` child container in that shared storage,
then idempotently ensures and locks its 90-day WORM policy. It does not create, update, or delete ADX
or any West US 2 resource.
`deploy-analytics` is a separately reviewed ADX-only operation that
cannot change Foundry models, Projects, telemetry, storage, or registries. Neither command creates a
native ADX dashboard because that dashboard surface has no ARM/Bicep deployment resource.

Render the private, ready-to-import dashboard file after deployment:

```powershell
python -m agent_insights_quality render-adx-dashboard
```

In the ADX web UI, use **New dashboard** > **Import dashboard from file** and select the rendered file
under `~/.aiq-runtime/agent-insights-quality/dashboards/`. The rendered file contains private Azure
context and must never be committed. Daily email uses the reviewed public
`https://aka.ms/agent-insights/quality` short link, which redirects to the shared dashboard.

Provisioning reconciles five stable Agents, all 41 logical authorities to their exact returned
provider versions and content digests, and five disabled/manual monitors in the selected Daily
profile. Every hosted version must activate and bind an exact-version session. Pure
Prompt traffic uses an exact Agent reference, declares no tools or tool fixtures, and fails closed if
the model emits a function call. Provisioning emits flushed, public-safe phase, version,
activation, retry, image-cache, monitor, and registry progress without exposing private resource IDs.

All potentially long-running repository operations use the same console contract: emit a public-safe
start line, a periodic elapsed heartbeat, and a completion or failure line. This includes
infrastructure and ADX deployment, Azure/profile/registry/work-item reads, image build and push,
provisioning, cleanup, endpoint traffic, telemetry queries, trace waits, and Agent Insights runs.
Progress-output failures are best-effort and never fail the underlying operation.

Each issue folder is self-contained. Prompt issues deploy their complete `definition.json`; Hosted
issues package their complete `source/` tree together with the shared requirements and host/container
contract. A deployed issue version contains only its reviewed defect and no dormant branches for
other issues, so source-aware proposed fixes see the exact defective implementation.

Official staging qualification uses `run-test-agent-validation` and always runs the full
41-authority matrix for one exact commit in the durable `aiq-staging-swedencentral` Project. Daily derives the
exact-head approved-record Blob path and reads that immutable authority directly; it never trusts an
operator-supplied local record file. Daily rejects a missing record, invalid WORM metadata, commit
drift, or validation-digest drift. After provisioning, verify the registry and endpoints read-only.
Do not send Daily smoke traffic.

Provisioning writes each profile registry locally and uploads
`swedencentral-g30/daily.json` or `swedencentral-g30/staging.json` to the private Azure
`deployment-registries` container using Entra authentication. Every qualification run
downloads the canonical blob before traffic, then validates profile, Project, catalogs, and all
version digests. Authorized operators therefore share one deployed environment without committing
Azure deployment identifiers to Git.

## Daily execution

```powershell
python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

Before traffic, Daily performs a read-only ARM GET of its concrete Foundry Project and resolves the
returned `location` through Azure location metadata. That live Project is the sole region source; the
deployment registry is only a required normalized cross-check and is never a fallback. The current
canonical display is `SwedenCentral`. Report/email generation fails if the live location, metadata display,
or registry match is missing. This contract is intentionally one scalar region: no experiments,
region arrays, comparison runs, or region-scoped report directories.

The runner validates catalog hashes against the protected daily registry, resets each monitor once,
runs `v0`, then runs four deterministic issues per Agent without an unconditional pre-traffic sleep.
Agent starts are staggered by five seconds to avoid a simultaneous endpoint burst while all
five Agents still execute concurrently; exact versions for one Agent execute sequentially. Before each
Hosted version, the runner patches the Agent endpoint to one `FixedRatio` rule with 100% traffic on
that exact version, confirms the selector, and then creates an exact-version session. This keeps
compute behavior and outer telemetry version identity aligned.

Daily and staging Projects prohibit ad-hoc debug traffic. Monitor reset does not delete telemetry.
Debug locally or in a separately owned sandbox. Qualification evidence is accepted only when exact
run, Agent, provider-version, operation, and invocation-time-window correlation hydrates and remains
stable within the bounded post-invoke deadline.

The runner prints flushed, thread-safe progress lines for each Agent/version and for endpoint,
telemetry, trace, and Agent Insights stages. Long telemetry waits, Insight runs, and remote retries
emit periodic heartbeats without exposing URLs, payloads, or private identifiers.

Every Hosted baseline and issue polls exact response-to-operation correlation every 15 seconds through
the existing 15-minute bounded ingestion deadline, whether or not its traffic declares trace
assertions. At the first complete exact mapping, the runner starts and persists the one Agent Insights
run immediately while every operation is still inside the guarded `0.1`-hour lookback window. Cards
remain quarantined until the same mapping, optional assertion outcomes, and correlated rows stay
unchanged for the reviewed 180-second ingestion interval. This interval exceeds the reproduced
135-second late-span delay by three poll periods and is intentionally separate from the 10-minute
traffic uncertainty horizon. New evidence resets stabilization. Missing or failing assertion evidence
waits for the deadline, and only a stable failure can return there; an unstabilized pass is rejected.
A late operation sharing a response identity makes correlation ambiguous, saves the baseline or issue
as incomplete, and drains the already-started run without persisting or scoring its cards. Exact
Agent, Foundry-version, invocation-time, operation, and foreign-card subset filtering remain
mandatory. Hosted baseline terminal and tool behavior is validated only after correlation stabilizes;
the Prompt path does not enter this Hosted stage.
Recovery claims are durably capped per Agent across resumes. A quarantined Insight run must drain
before later versions for that Agent can send traffic; an unresolved start without a run ID fails
closed on resume instead of sending new target traffic. The immutable run manifest is deferred
until no start or drain quarantine remains.

Issue source, traffic, source-delta manifests, and version digests are reviewed contracts. Equal
nonzero request, response, and usable-response counts plus a verified natural trace contract prove
endpoint execution. A baseline additionally requires all reviewed deterministic assertions and one
terminal success plus output-presence signal per request. Prompt baselines require exactly one direct
terminal response and no function calls per request. A failed baseline assertion or designated issue
activation assertion makes the evidence incomplete rather than an Insight Engine miss.
Prompt baselines retain strict structured-output requests where required for deterministic stability.
Prompt issue activation requests never constrain endpoint output with a response schema; evaluator-side
`exact_json` assertions verify the response without manufacturing the defect under test.
Handled child errors require an independently successful terminal response; unhandled baseline
errors always keep the run incomplete.

The measured five-Agent-concurrent daily smoke on 2026-08-25 completed in 37.6 minutes: 32.1 minutes
for endpoint/telemetry/Agent Insights runtime, 5.3 minutes for parallel Sol assessment, and 13 seconds
for finalization. That measurement predates mandatory stabilization for assertion-free Hosted
invocations. Five sequential daily versions reserve 15 minutes of correlation guarding per Hosted
Agent lane, and nine full-catalog versions reserve 27 minutes; the three Hosted lanes remain
concurrent, and the guard overlaps the Agent Insights run started at the first mapping. Allow a
60-minute no-retry daily planning budget rather than treating 37.6 minutes as a current upper bound.
Service retries can extend runtime beyond that budget.

An isolated issue failure does not stop later issues. Ambiguous later evidence remains
`INCOMPLETE`; it is never converted into a product miss.

A baseline false-positive Insight contributes to the unified `noise_cards` penalty but does not stop
issue diagnostics. Only an operational, telemetry, provenance, or trace-contract baseline failure
stops that Agent.

## Assessment and finalization

`run-daily` writes private assessment packages beside `run-manifest.json`. Packages contain
privacy-safe per-request assertion outcomes, full-request trace proof, and separate card-linked trace
proof. Use GPT-5.6 Sol with
`src/agent_insights_quality/prompts/assessment.md`, then finalize:

Run manifest schema `5.0.0` binds the live Project region, registry cross-check, both the
official/test-email delivery mode, and verified source,
activation, endpoint, semantic, and trace evidence. Superseded manifest shapes are rejected.
Newly finalized reports use schema `2.0.0` and require verified source integrity for every complete
`PASS` or `FAIL`; immutable historical reports are not rewritten or accepted as current output.

```powershell
python -m agent_insights_quality finalize `
  --manifest <private-run-manifest> `
  --assessment <issue-001-assessment.json> `
  --assessment <...> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

Finalization writes sanitized per-Agent Markdown under the report's `agents/` directory. Each row is
one generated Insight card, plus an explicit row for each missing expected issue. Email links to these
GitHub-rendered Markdown reports; private prompts, responses, traces, and resource identifiers remain
excluded. Each report includes a review summary, evaluation legend, and human-validation checklist.
Complete email, preview, aggregate Markdown, and per-Agent Markdown surfaces show the numeric score,
comparison, and finding details without displaying the internal `PASS` or `FAIL` status. They keep
`INCOMPLETE` prominent because it indicates an unsafe evidence state rather than a quality verdict.
Finalization also writes private `report-preview.html` beside the run manifest. Staging human review
uses this preview because it is rendered by the exact same Outlook-safe HTML path as the daily email.
It may contain private work-item context and runtime links, so it must never be committed.

For Daily, first run `finalize --prepare-improvement-input` after assessments. Give only that
public-safe normalized file to GPT-5.6 Sol with
`src/agent_insights_quality/prompts/improvement.md`, then rerun finalization with
`--improvement-analysis <private-json>`. Schema and citation validation reject any pattern not backed
by at least two distinct Agents with `insight_engine` ownership and exact normalized finding IDs.
Deterministic code replaces model-local pattern keys with stable IDs and owns new/active/watching/
resolved/reopened transitions. An incomplete or incomparable run preserves the visible prior status
and records `not_evaluated` without advancing absence. The complete Daily report, stable
JSON/Markdown, and full immutable dated analysis snapshot are staged and validated before
retry-safe publication; latest views and trend then join the same generated-only pull request.
Optional email-only tests write only a private improvement preview.

For `daily`, finalization also derives one public-safe payload and publishes it atomically to ADX.
The v2 payload exposes logical `AIQDailyRuns`, `AIQDailyAgents`, `AIQDailyBaselines`,
`AIQDailyIssues`, `AIQDailyCards`, `AIQDailyFields`, and `AIQDailyHighlights` views. It contains the
20 tested issues, matching public catalog expectations, current maintenance owners, outcome and
field detail, generated-card metadata, full reasoning already present in the committed sanitized
report, and the same aggregate highlights rendered in email. Historical publication resolves the
exact reviewed catalog snapshot from the report's publishing commit before attaching expected root
cause and fix text.

ADX never receives private assessment packages, Azure Boards work items, prompts, responses, raw
traces, evidence references, provider IDs, Foundry versions, private Project/Agent links, or Azure
resource identifiers. `run_id`, payload version, and a deterministic source digest make retries
idempotent and reject conflicting content. Canonical functions read only v2 rows; they do not
translate the superseded v1 payload.

An ADX failure does not change the qualification result or block email and generated pull-request
publication. Finalization writes an explicit private failure receipt, returns the failure code, and
adds a warning to the HTML email. The pull-request description and final automation result must also
state the ADX status.

The direct-email request remains private. Use the available Copilot email capability exactly once and
set HTML mode explicitly. Never create a draft or retry an ambiguous send. Import one receipt with
status `sent`, `failed`, or `unknown`; ambiguous delivery sets `retry_allowed=false` and requires
manual verification.

Every daily email includes the reviewed `https://aka.ms/agent-insights/quality` quality-trend link.
Repository validation rejects any unreviewed replacement.

Before finalization, fetch the privately configured Azure Boards saved query:

```powershell
python -m agent_insights_quality fetch-quality-work-items `
  --query-url <private-query-url> `
  --report-date <Pacific YYYY-MM-DD> `
  --output $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

Pass that snapshot to both the runner and `finalize` with `--work-items`. The email first lists active
exact-`Quality` items, then items closed on the previous Pacific date, using ID, type, title, assignee,
and state. `Removed` items are excluded. The query URL, snapshot, and work-item content remain private
and never enter generated reports. Before traffic, the runner validates the closed date and writes a
private digest binding beside the run manifest; finalization rejects a different or stale snapshot.

```powershell
python -m agent_insights_quality email-receipt-import `
  --request <private-request> --receipt <provider-receipt> `
  --output reports/daily/YYYY/MM/DD/email-receipt.json
```

## Replay

Read-only replay validates the immutable manifest and retained evidence coordinates:

```powershell
python -m agent_insights_quality replay-run --manifest <private-run-manifest>
```

Replay does not reset monitors, send traffic, mutate Insights, or write telemetry. A true reproduction
is a new run with new endpoint traffic and a new run identity.

## Generated pull requests

Generated branches use `aiq-daily/`. Only new-format report, latest, trend, and email-receipt files
are allowed. Trusted `PASS` and `FAIL` reports enable auto-merge after required checks.
`INCOMPLETE` reports do not auto-merge.

## ADX backfill and retry

Publish one or more validated daily reports explicitly with repeatable `--report` arguments:

```powershell
python -m agent_insights_quality publish-adx `
  --report reports\daily\YYYY\MM\DD\report.json
```

To initialize history, invoke the command for every existing daily report that passes the current
report contract, in chronological order. Never adapt or restore superseded report formats solely for
ADX backfill. An identical retry returns `already_published`; a different v2 payload for the same
`run_id` fails closed. Publication receipts remain private under
`~/.aiq-runtime/agent-insights-quality/adx-publications/`.
