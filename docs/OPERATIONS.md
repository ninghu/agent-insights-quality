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
Project, exact GPT-5.4 mini deployment, shared ACR, dedicated Sweden `g30` storage, and read-only
Sweden `g30` telemetry. It
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
concurrency. Keep no more than eight visible Copilot sub-sessions active in any deployment, invocation,
or verification phase, and allow one scenario attempt per runtime. The shared bucket charges each
request's input plus maximum output budget, multiplied by reviewed inner fan-out, and a provider
`Retry-After` pauses every consumer. Every issue and paired-v0 attempt gets a globally unique execution,
conversation, session, response, and operation identity while receiving the same matrix. Evidence
binds the exact deployed Agent/version and derives identity from correlated telemetry.
Semantic and trace assertions are evaluated only by GPT-5.6 Sol from a validation-private,
content-addressed package. Deterministic code validates complete evaluation coverage and aggregates
the returned per-step and per-attempt booleans against the reviewed thresholds; it does not interpret
behavior through issue- or assertion-specific runtime parsers.

The coordinator lifecycle is:

```text
LOCKED -> PREFLIGHT -> CREATING -> VALIDATING -> READY | FAILED
                                  \-> SUPERSEDED
```

Each validation authority has one unique runtime Agent identity and is an indivisible deployment
assignment. The visible Copilot coordinator computes changed authorities from exact source and
provider-content digests, publishes content-addressed immutable desired-state and phase assignments,
releases the global lock, and remains responsive. It creates visible Copilot sub-sessions for all
parallel deployment, invocation, and verification work. Never replace those sub-sessions with
subprocesses, `ThreadPoolExecutor`, or any other hidden in-process pool.

No more than eight deployment sub-sessions may be active at once. Each owns a disjoint immutable
assignment, may exact-reuse or deploy only its assigned version, and writes an immutable per-authority
readiness receipt. Sub-sessions never mutate shared lifecycle, topology, or registry state.
Fine-grained authority and shard locks plus generation fencing prevent duplicate deployment and make
stale sub-sessions fail closed.

After every required deployment receipt exists, the coordinator centrally re-reads all 41 exact
Agent versions, verifies the durable Project, read-only telemetry binding, and zero-monitor
invariant, merges changed and reused readiness proofs, then atomically publishes the single
topology and deployment registry. Validation selection is separate: an authority runs only when
its binding changed, its latest result is `INCOMPLETE`, or its exact result is missing. A definitive
`FAIL` is a completed result and is not selected again unless its binding changes. Every issue
selected for new traffic still carries a fresh paired-`v0` control.

Deployment and invocation each receive an independent deterministic assignment set with one to eight
cost-balanced logical shards. Each active shard maps 1:1 to one visible Copilot sub-session.
Verification begins only after the invocation barrier, uses one visible GPT-5.6 Sol verifier session
at a time, and never invokes an endpoint.

Immediately after one authority reaches definitive completion, its sub-session atomically publishes a
generation-fenced invocation receipt instead of waiting for the shard to finish. The receipt binds the
exact Agent source digest, provider-content digest, traffic-generation/execution digest,
provider-assigned version, runtime mapping, environment, Project, telemetry resource-set identity,
response and session references, invoke and evidence windows, complete issue and paired-`v0`
provenance, source-artifact schema and version, origin run/commit/generation/shard, and its own
immutable artifact digest.

Unknown, ambiguous, duplicate, partial, or indeterminate retried-POST outcomes never produce reusable
completion. Cross-generation reuse performs one atomic, generation-fenced extraction from the
immutable source receipt; stale sub-sessions cannot extract or publish it. The next generation
therefore selects only changed, `INCOMPLETE`, or missing authorities. Within that set, it
invokes only authorities without a current exact-bound completed receipt; all others receive
verification-only assignments and send no new endpoint traffic.

Receipt reuse proves only that the traffic-generation and execution binding is unchanged. Every new
verification package must separately bind the reused receipt's immutable digest and the current
verifier commit and verifier digest. Verifier-only changes can therefore re-evaluate exact completed
traffic without treating old verifier output as current.

### Copilot per-authority verification

One visible GPT-5.6 Sol verifier session claims one generation-fenced authority at a time from the
immutable assignment. The package and import primitives are sequential: they start no subprocess,
thread, task pool, or other internal concurrency, accept no generation or authority identity, resolve
the hidden active generation themselves, and never deploy, invoke, or send endpoint traffic.

For one claimed baseline authority, the verifier issues one batched telemetry query and waits for one
stability snapshot containing all five response-bound attempt trees. For one claimed issue authority,
it performs exactly two independent target batches: one batched stability snapshot containing all
issue attempts and one containing all paired-`v0` attempts. It never queries or stabilizes individual
attempts as separate verification units. The snapshots remain bound to the exact receipt,
response/session references, invoke/evidence windows, runtime, Project, and telemetry resource set.

Deterministic code enforces exact repository, PR, source, provider, runtime, environment, Project,
telemetry, invocation-receipt, response/session, endpoint-output, trace-subtree, privacy, and package
bindings. GPT-5.6 Sol evaluates all behavioral assertions and per-attempt observations through the
strict validation evaluation schema. Code then assigns exactly one authority result:

| Result | Meaning |
| --- | --- |
| `PASS` | Complete stable evidence meets the reviewed threshold. |
| `FAIL` | Complete stable evidence does not meet the reviewed threshold. |
| `INCOMPLETE` | Required evidence is missing, ambiguous, partial, or not stable by the deadline. |

The reviewed thresholds are baseline `5/5`, deterministic issue `5/5` with paired `v0` `0/5`, and
model-mediated issue `>=5/7` with paired `v0` `0/7`. `INCOMPLETE` is never collapsed into `FAIL`.
Immediately after deciding an authority, the sub-session atomically persists an immutable,
generation-fenced result before claiming another authority. A later authority failure, sub-session
failure, or composition interruption does not discard completed authority results.

A retry assignment contains only authorities whose result is missing or `INCOMPLETE`, plus
authorities whose exact source/content/execution/runtime/verifier binding changed. Definitive
unchanged `PASS` and `FAIL` results are reused. Composition requires exactly one current result for
all 41 authorities: all `PASS` produces `READY`, any definitive `FAIL` produces `FAILED`, and any
missing or `INCOMPLETE` authority keeps the generation incomplete and retryable.

Support images use content-addressed ACR identities and exact digest pins. Agents, versions,
sessions, responses, images, telemetry, registries, and evidence are retained. Old objects cannot
contaminate a later result because every request is correlated to its exact response-bound
`invoke_agent` anchor and complete descendant span tree. Missing, ambiguous, stale, orphaned, cyclic,
duplicate, conflicting, or cross-root bindings fail closed. Final composition accepts only exact
current authority results covering all 41 authorities.

A new internal generation atomically writes a `SUPERSEDED` event and swaps the active pointer. Its
opaque identity is system-created, is never supplied through the CLI, and is never encoded in
Project or Agent names. A legacy active record is archived byte-for-byte and referenced only by an
opaque tombstone digest, never interpreted by a compatibility reader. There is no validation cleanup:
provider sessions, responses, Agents, versions, identities, deployments, images, telemetry,
registries, receipts, and evidence remain durable.

The visible Copilot coordinator runs preparation:

```powershell
python -m agent_insights_quality prepare-test-agent-validation
```

Preparation discovers the repository, open PR, exact clean commit, and private runtime paths, then
publishes the hidden active generation and its immutable deployment assignments. For each assignment,
the coordinator creates one visible sub-session and gives it exactly one primitive command:

```powershell
python -m agent_insights_quality deploy-test-agent-validation-shard --shard-id <N>
```

After all deployment sub-sessions finish, the coordinator reconciles centrally:

```powershell
python -m agent_insights_quality reconcile-test-agent-validation-deployment
```

Reconciliation publishes the selected invocation and verification assignments. The coordinator
creates one visible invocation sub-session per assignment and gives each exactly one command:

```powershell
python -m agent_insights_quality invoke-test-agent-validation-shard --shard-id <N>
```

After the invocation barrier, use fresh or reusable completed invocation receipts in one visible
GPT-5.6 Sol verifier session. Prepare or locate the next private package:

```powershell
python -m agent_insights_quality prepare-test-agent-validation-assessment
```

Read the reported package and validation-specific prompt locally, write strict public-safe JSON to
the reported assessment path, then validate, import, and persist the schema-1.0 authority result:

```powershell
python -m agent_insights_quality import-test-agent-validation-assessment
```

Repeat the two commands one authority at a time. The package, raw output, tool data, and traces never
leave the durable private runtime root and must not appear in CLI output or cross-session messages.
After all authority evaluations finish, the coordinator composes the exact 41-authority result:

```powershell
python -m agent_insights_quality compose-test-agent-validation
```

The CLI never accepts a run ID, generation ID, or authority ID for these primitives. Each resolves
the hidden active generation and its immutable assignment. Interrupted work resumes from
exact-bound receipts; stale sub-sessions cannot publish. Starting a new generation supersedes prior
incomplete state without deleting retained provider resources or sending traffic for already
completed exact-bound invocations.

Use the status entry point only for status and next-action guidance:

```powershell
python -m agent_insights_quality run-test-agent-validation
```

It never creates sub-sessions or launches hidden workers.
After the user explicitly approves that exact result, run:

```powershell
python -m agent_insights_quality approve-test-agent-validation
```

The approval command re-reads the exact PR head and 41-authority READY evidence, then create-once
writes one minimal immutable approved record bound to the evidence and generation digests. It has
no cleanup, workflow, App, check, tree, topology, quota, or local-path fields. GitHub runs ordinary
mechanical CI only and merge remains manual.

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
Project-scoped `g30` telemetry pairs, exact reviewed model deployments, and the dedicated
`aiqsweart${uniqueSuffix}` Sweden StorageV2 account in the existing resource group. The new account
uses Blob versioning and private `quality-artifacts`, `deployment-registries`, and
`test-agent-validation-approved-records-swedencentral-g30` containers. Its 90-day lifecycle rule is
scoped only to `quality-artifacts`; the approved-record WORM policy is ensured and locked
idempotently after deployment. The command references the shared ACR but does not create, update, or
delete ADX, the retained legacy storage account or its data, or any other West US 2 resource.
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

Official staging qualification uses the five coordinator primitives and always composes the full
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

An isolated issue failure does not stop later issues. Ambiguous later evidence remains an internal
qualification failure; it is never converted into a product miss or a published report.

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
Newly finalized reports use schema `3.0.0`, always contain a numeric score, and require verified
source integrity. Immutable historical reports are not rewritten or accepted as current output.

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
same-formula comparison, and finding details without a status or threshold. Incomplete evidence
produces no report surface.
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
are allowed. A complete schema-valid numeric report enables auto-merge after required checks.
Incomplete execution creates no generated branch.

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
