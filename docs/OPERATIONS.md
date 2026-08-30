# Operations

## Environment model

`daily` and `staging` are isolated Foundry accounts and profiles. Never point one profile at the
other profile's account, Project, Application Insights resource, deployment registry, monitor, or
private artifact root.

| Profile | Project | Purpose |
| --- | --- | --- |
| `daily` | `agent-insights-quality` | Weekday qualification |
| `staging` | `agent-insights-quality-staging` | Full qualification before promotion |

`r03` is the final legacy staging run. The persistent staging row remains only while the external
new-only Daily receipt cutover is pending; do not start another staging run or use it as fallback.

## Test Agent Validation

Test Agent Validation reuses the staging account, GPT-5.4 mini deployment, ACR, storage, and read-only
`g29` telemetry, but creates one opaque temporary Project and 41 independent Agent endpoints per
candidate. It creates no monitor and does not run Agent Insights, Sol assessment, score/report
generation, ADX publication, email, or Daily.

The executable authority is each `traffic.json` `validation_rules` contract. Its automatic digest
includes complete setup/probe request bodies, ordered conversation grouping, parameters, fixtures,
semantic/trace/identity assertions, reviewed mode, `n`, `k`, runtime kind, framework, and model
contract. Baseline is `5/5`; deterministic issues are `5/5` with paired-v0 `0/5`; model-mediated
issues are `5/7` with paired-v0 `0/7`. `minimum_traces` keeps its Daily meaning and is never used as a
validation attempt threshold.

Before any Project create, acquire the account-wide infinite lease and record measured RPM/TPM,
25-percent and absolute headroom, the complete endpoint envelope, inner model fan-out, and bounded
concurrency. Provision at most eight, query telemetry at most four, and allow one scenario
attempt per runtime. Every attempt gets a fresh conversation identity; issue and v0 receive the same
matrix. Evidence stores completion separately from a nullable defect observation.

The lifecycle is:

```text
LEASED -> PREFLIGHT -> CREATING -> VALIDATING -> FROZEN
  -> (REVIEWED | SHADOW_REVIEW_SKIPPED) -> REVALIDATING
  -> FINAL_CHECKS -> CLEANING -> CLEAN -> RECEIPT_ISSUED
```

Every mutation writes an immutable event snapshot first and then updates the active journal with the
lease plus current ETag. A stale/expired cycle can be taken over only by the cleanup reconciler using
a fresh lease ID, nonce, and epoch. The 72-hour expiration never extends.

Cleanup records intent first and deletes responses, conversations/sessions, Agent versions and
Agents, Hosted deployments/identities/blueprints, connections, role assignments, cycle principals,
ACR tags and unshared manifests, then the Project. Final proof requires Project `404`, no nonce-owned
resources, sessions/responses, cycle tags, or incomplete reviewed cascades. Ambiguity enters
`CLEANUP_BLOCKED` and keeps the account unavailable.

Shadow receipts bind candidate-head policy and always set `authorizes_merge=false` and
`default_branch_trust_anchor_present=false`. Only the protected default-branch issuer can create a
merge receipt, after re-querying the exact final PR head/tree, trusted policy/workflow/App/environment,
one comprehensive review, targeted verification, CI, 41/41 evidence, and immutable `CLEAN` snapshot.
Receipt creation uses `If-None-Match:*`; an existing different digest fails closed.

### External Daily receipt cutover

Test Agent Validation does not change Daily ownership. The separate Daily owner must first deploy and
dry-run a **new-only** validation-receipt consumer; no process may read both legacy promotion receipts
and validation receipts. Under the Daily quiescence lock, prove no run/provision/publication is active,
pause the scheduler, switch the active source atomically, and remain new-only on failure.

After re-verifying default-branch policy/workflow/App/check provenance, 41 current digests, and the
immutable `CLEAN` snapshot, provision Daily once and perform only read-only readiness and registry
reconciliation. Do not send smoke traffic. For this migration only, after readiness succeeds, the
Daily owner may run the explicitly requested isolated `--test-run --rerun N` email-only Daily Test.
It is external, non-gating, and writes no ADX, official report/trend, or pull request. Only then may
legacy staging resources and code be removed forward-only; staging is never a fallback.

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
python -m agent_insights_quality provision --profile staging
```

The full infrastructure command includes a two-node production ADX cluster and the
`AgentInsightsQuality` database in `agent-insights-quality-rg`. Use `deploy-analytics` for an
ADX-only deployment that cannot change Foundry models, Projects, telemetry, storage, or registries.
Neither command creates a native ADX dashboard because that dashboard surface has no ARM/Bicep
deployment resource.

Render the private, ready-to-import dashboard file after deployment:

```powershell
python -m agent_insights_quality render-adx-dashboard
```

In the ADX web UI, use **New dashboard** > **Import dashboard from file** and select the rendered file
under `~/.aiq-runtime/agent-insights-quality/dashboards/`. The rendered file contains private Azure
context and must never be committed. Daily email uses the reviewed public
`https://aka.ms/agent-insights/quality` short link, which redirects to the shared dashboard.

Provisioning creates five Agents, 41 immutable versions, and five disabled/manual monitors in exactly
one selected profile. Every hosted version must activate and bind an exact-version session. Pure
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

The following command is retained for legacy migration history only and must not be run after `r03`:

```powershell
python -m agent_insights_quality run-full --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
```

For an isolated Agent-only change, qualify only that Agent's `v0` and all assigned issues. Existing
Agents may reuse their latest reviewed evidence only when their content digests, mappings, and all
shared contracts are unchanged. Shared runtime, telemetry, assessment, scoring, schema,
infrastructure, or cross-Agent topology changes require full-catalog qualification or read-only
re-evaluation of retained evidence when no new traffic is needed.

After a complete staging `PASS` or `FAIL` report is human-reviewed, bind all 41 exact content
digests. `INCOMPLETE` is never promotable:

```powershell
python -m agent_insights_quality create-promotion-receipt `
  --report <private-staging-report> --manifest <private-staging-manifest> `
  --registry <private-staging-registry> `
  --output <private-promotion-receipt> --human-reviewed
```

Set `AIQ_STAGING_PROMOTION_RECEIPT` to that private file before provisioning `daily`.

Promotion may compose newly reviewed affected-Agent evidence with the latest valid receipts for
unchanged Agents. The composed receipt must bind every current mapping and exact digest; incomplete
evidence is never reusable. After daily provisioning, verify the registry and endpoints read-only.
Do not send smoke traffic that starts a new clean-window wait.

Provisioning writes each profile registry locally and uploads `daily.json` or `staging.json` to the
private Azure `deployment-registries` container using Entra authentication. Every qualification run
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
canonical display is `WestUS2`. Report/email generation fails if the live location, metadata display,
or registry match is missing. This contract is intentionally one scalar region: no experiments,
region arrays, comparison runs, or region-scoped report directories.

The runner validates catalog hashes against the protected daily registry, resets each monitor once,
waits for the reviewed `0.1`-hour clean interval, runs `v0`, then runs four deterministic issues per
Agent. Agent starts are staggered by five seconds to avoid a simultaneous endpoint burst while all
five Agents still execute concurrently; exact versions for one Agent execute sequentially. Before each
Hosted version, the runner patches the Agent endpoint to one `FixedRatio` rule with 100% traffic on
that exact version, confirms the selector, and then creates an exact-version session. This keeps
compute behavior and outer telemetry version identity aligned.

Daily and staging Projects prohibit ad-hoc debug traffic. A pre-existing `invoke_agent` trace in the
minimum lookback window delays the Agent before any qualification traffic is sent. Monitor reset does
not delete telemetry. Debug locally or in a separately owned sandbox; the runner waits until the
short clean interval expires before rerunning qualification against the same profile.

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
before later versions for that Agent can send traffic; an unresolved start without a run ID requires
a clean-window reset on resume instead of new target traffic. The immutable run manifest is deferred
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
by at least two distinct Agents with `insight_engine` ownership. Deterministic code owns new/active/
watching/resolved/reopened/not-evaluated transitions. The stable JSON/Markdown, immutable dated
snapshot, Daily report, latest views, and trend are submitted in one generated-only pull request.
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
