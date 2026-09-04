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
contract. Baselines, issues, and paired controls each use ten attempts and require six complete
role-specific passes. The remaining four attempts remain transparent misses of any kind and never
veto six strict passes. Endpoint, identity, semantic, trace, and internal consistency remain exact
requirements for every passing attempt.
Expected issue observations remain private-review context rather than a local verdict.
`minimum_traces` is the independent full-request natural trace proof threshold and is never used as
a validation attempt threshold or card-link coverage threshold. After independent Agent activation
is proven, one unique exact-version, response-bound, claim-relevant linked operation is sufficient to
prove card linkage; semantic correctness still depends on the card title, description, and category.

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
topology and deployment registry. Every fresh advisory run selects all 41 authorities. Every issue
selected for new traffic carries a paired-`v0` control.
Only an automatic successor recovery generation reuses the immediately preceding generation's
flattened definitive result and exact receipt references.

Deployment and invocation each receive an independent deterministic assignment set with one to eight
cost-balanced logical shards. Each active shard maps 1:1 to one visible Copilot sub-session.
Verification begins only after the invocation barrier, uses up to eight visible GPT-5.6 Sol evaluator
sessions, and never invokes an endpoint.

Immediately after one authority reaches definitive completion, its sub-session atomically publishes a
generation-fenced invocation receipt instead of waiting for the shard to finish. The receipt binds the
exact Agent source digest, provider-content digest, traffic-generation/execution digest,
provider-assigned version, runtime mapping, environment, Project, telemetry resource-set identity,
response and session references, invoke and evidence windows, complete issue and paired-`v0`
provenance, source-artifact schema and version, origin run/commit/generation/shard, and its own
immutable artifact digest.

Unknown, ambiguous, duplicate, partial, or indeterminate retried-POST outcomes never produce reusable
completion. Once every selected evaluation
finishes and any result is `INCOMPLETE`, the coordinator uses one explicit recovery command to
atomically advance and reconcile the same-head successor. The first exact-receipt recollection has no
deployment or invocation assignment. A repeated exact-receipt `INCOMPLETE` selects fresh issue plus
paired-`v0` traffic in the following generation. A mature trace-only unknown that satisfies the
shared acceptance policy is already definitive and does not enter recovery. All other selected
authorities receive verification-only assignments. Recovery receipt selection checks only the
flattened references in the immediately preceding generation's `recovery_source`; every candidate is
fully validated against exact source/provider/traffic identity. Immutable receipt files
remain the authority.

Receipt reuse proves only that the traffic-generation and execution binding is unchanged. Every new
verification package must separately bind the reused receipt's immutable digest and the current
verifier commit and verifier digest. Verifier-only changes can therefore re-evaluate exact completed
traffic without treating old verifier output as current.

### Copilot per-authority verification

Each visible GPT-5.6 Sol evaluator session claims one generation-fenced authority at a time from the
immutable assignment. The no-ID package and import primitives use a hidden deterministic worktree
reference: prepare resumes the caller's unexpired claim or atomically assigns a distinct authority,
while import resolves only that claim and releases its slot after persisting the immutable result.
If a visible evaluator must stop before import, it explicitly releases only its own current claim;
the atomic release is generation- and claimant-fenced, invalidates the stale package, and makes the
authority immediately claimable. Lease expiry remains the crash fallback. Shared-lock contention
before assignment returns a structured retryable busy result and never claims work. At most eight
claims are active, and status reports only aggregate active/available slots and the next prepare
command. The primitives start no subprocess, thread, task pool, or other internal concurrency and
never deploy, invoke, or send endpoint traffic.

For one claimed baseline authority, the verifier issues one batched telemetry query and waits for one
stability snapshot containing all five response-bound attempt trees. For one claimed issue authority,
it performs exactly two independent target batches: one batched stability snapshot containing all
issue attempts and one containing all paired-`v0` attempts. It never queries or stabilizes individual
attempts as separate verification units. An unchanged fresh partial batch does not start or satisfy
the normal stability interval until every required trace surface is present. If required traces remain missing at the bounded
deadline, the authority stays `INCOMPLETE` for verify-only recovery. The snapshots remain bound to the exact receipt,
response/session references, invoke/evidence windows, runtime, Project, and telemetry resource set.
For an exact reused receipt observed at or beyond its evidence-window end plus the configured maximum
hydration horizon and stability interval, discovery, trace collection, and identity checks each use
one whole-target query with no polling sleep. The private package binds the receipt digest, evidence
window, maturity boundary, as-of time, and timing policy. Complete mature evidence proceeds
immediately; a remaining required trace gap is recorded as an explicit unknown for the shared
reviewed acceptance policy.

Deterministic code enforces exact repository, PR, source, provider, runtime, environment, Project,
telemetry, invocation-receipt, response/session, endpoint-output, trace-subtree, privacy, and package
bindings. GPT-5.6 Sol evaluates all behavioral assertions and per-attempt observations through the
strict validation evaluation schema. Code then assigns exactly one authority result:

| Result | Meaning |
| --- | --- |
| `PASS` | Complete stable evidence meets the reviewed threshold. |
| `FAIL` | Complete stable evidence does not meet the reviewed threshold. |
| `INCOMPLETE` | Required evidence is missing, ambiguous, partial, or not stable by the deadline. |

The shared threshold is six complete role-specific passes: healthy baseline attempts, defect-observed
issue attempts, or zero-defect paired controls. The other four attempts never veto aggregate PASS and
retain their exact miss categories. `INCOMPLETE` is never collapsed into `FAIL`.
Immediately after deciding an authority, the sub-session atomically persists an immutable,
generation-fenced result before claiming another authority. A later authority failure, sub-session
failure, or composition interruption does not discard completed authority results.

The current generation is the only authority for a fresh advisory run. Recovery reads one
integrity-bound `recovery_source` reference containing flattened deterministic per-authority result
and receipt paths. It does not scan or rebuild global history. A retry assignment contains only
incomplete authorities. Strict policy relaxation may migrate a
still-valid PASS; tightening or semantic changes reuse an exact receipt for verification and never
send Agent traffic. Composition requires exactly one current result for all 41 authorities: all
`PASS` produces `READY`, any definitive `FAIL` produces `FAILED`, and any missing or `INCOMPLETE`
authority keeps the generation incomplete and retryable. Status lists all 41 authorities with
public-safe state and identity-change reasons.

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

After the invocation barrier, use fresh or reusable completed invocation receipts in up to eight
visible GPT-5.6 Sol evaluator sessions. In each session, prepare or locate its next private package:

```powershell
python -m agent_insights_quality prepare-test-agent-validation-assessment
```

Read the reported package and validation-specific prompt locally, write strict public-safe JSON to
the reported assessment path, then validate, import, and persist the schema-1.0 authority result:

```powershell
python -m agent_insights_quality import-test-agent-validation-assessment
```

If the evaluator definitively stops before import, release its own claim:

```powershell
python -m agent_insights_quality release-test-agent-validation-assessment
```

Repeat the two commands one authority at a time per session. The package, raw output, tool data, and
traces never leave the durable private runtime root and must not appear in CLI output or cross-session
messages.
After all selected authority evaluations finish, run the explicit recovery primitive whenever status
reports `verification_incomplete`:

```powershell
python -m agent_insights_quality recover-test-agent-validation
```

The command writes one immutable `recovery_source`, reconciles the successor, reuses every exact
definitive result and eligible receipt, and publishes only the necessary visible evaluator or fresh
invocation assignments. Repeat this normal recovery loop until all authorities are definitive. Then
the coordinator composes the exact 41-authority result:

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

It never creates sub-sessions or launches hidden workers. GitHub runs ordinary mechanical CI only
and merge remains manual.

### Advisory staging review and Daily admission

Test Agent Validation is advisory input to human judgment, not a Daily admission artifact. Daily
never reads staging lifecycle state or a validation digest. A human decides whether to proceed and
represents that decision solely by manually invoking `daily-prepare`; there is no approval flag,
switch, or record.

`daily-prepare` still fails closed unless the checkout is clean and binds that exact commit together
with the catalog hashes, deterministic selection, reviewed policy, immutable work-item snapshot, and
hidden run identity. `daily-provision` then performs read-only readiness and exact Daily registry and
Project reconciliation without smoke traffic. The isolated `--test-run --rerun N` email-only Daily
Test uses the same admission contract and remains non-publishing. GitHub preview publication is a
separate reviewed opt-in through `--publish-preview`; it is invalid outside a nonzero email-only
rerun.

Daily and staging registries remain isolated: no process may read both as an admission source.
Daily run-scoped lifecycle and evidence bind only the exact Daily registry selected during
provisioning. The coordinator quiescence lock remains independent of advisory staging state.

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
uses Blob versioning and private `quality-artifacts` and `deployment-registries` containers. Its
90-day lifecycle rule is scoped only to `quality-artifacts`. The command references the shared ACR
but does not create, update, or
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
41-authority matrix for one exact commit in the durable `aiq-staging-swedencentral` Project. Its
results are advisory and remain separate from Daily admission. Daily reads no staging record or
validation digest. After provisioning, verify the Daily registry and endpoints read-only. Do not
send Daily smoke traffic.

Provisioning writes each profile registry locally and uploads
`swedencentral-g30/daily.json` or `swedencentral-g30/staging.json` to the private Azure
`deployment-registries` container using Entra authentication. Every qualification run
downloads the canonical blob before traffic, then validates profile, Project, catalogs, and all
version digests. Authorized operators therefore share one deployed environment without committing
Azure deployment identifiers to Git.

## Daily execution

```powershell
python -m agent_insights_quality daily-prepare --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
python -m agent_insights_quality daily-provision
python -m agent_insights_quality daily-guide
```

`daily-prepare` opens one private lifecycle/quiescence claim and binds the Pacific business date,
immutable work-item snapshot digest and closed-business date, exact clean checkout commit,
deterministic selections, catalog hashes, reviewed limits, and a hidden system-generated execution
identity. It does not fetch or bind Test Agent Validation state or a validation digest.
`daily-provision` performs new-only reconciliation, then freezes the exact Daily registry, Project
topology, promoted content/version mappings, and live region proof. No other Daily run may provision,
send traffic, compose, finalize, publish, or claim delivery until that lifecycle completes.

Before traffic, Daily performs a read-only ARM GET of its concrete Foundry Project and resolves the
returned `location` through Azure location metadata. That live Project is the sole region source; the
deployment registry is only a required normalized cross-check and is never a fallback. The current
canonical display is `SwedenCentral`. Report/email generation fails if the live location, metadata display,
or registry match is missing. This contract is intentionally one scalar region: no experiments,
region arrays, comparison runs, or region-scoped report directories.

Daily uses five visible whole-Agent lanes. Run `daily-run-agent --agent <name>` once per pending
Weather, Healthcare, Finance, Travel, and Support lane. The lanes may run concurrently, while each
lane processes `v0` then four issues in forward order. A version completes endpoint traffic,
whole-target telemetry/trace verification, Agent Insights, and immutable result publication before
the reviewed 60-second pacing and the next version. Daily never adds paired-`v0` issue traffic.

Every Agent/version has its own execution claim, short publication lock, exact traffic receipt, and
immutable terminal result. Network I/O and artifact construction never hold the coordinator lock.
The coordinator lock performs only short lifecycle CAS transitions. Status and composition derive
lane progress from immutable version artifacts, so a crash after receipt/result publication never
causes endpoint retraffic and does not require every lane to update shared aggregate state.

Telemetry maturity is bounded to five total minutes from immutable `traffic_completed_at`. Apply the
age-aware hydration grace, query the whole target adaptively, emit public-safe heartbeats, and require
a short consecutive-snapshot stability interval. At the boundary, one final exact snapshot may pass
immediately when complete and unambiguous. `operation_Id` bounds each trace; the expected response,
Agent/version, `span_id`, and `parent_span_id` select the exact `invoke_agent` root and descendants.
Foreign identities and byte-identical duplicate rows are ignored; distinct exact roots remain
ambiguous.

All ten attempts are retained. Six complete role-specific passes determine aggregate eligibility:
healthy baseline attempts or defect-observed issue attempts. The remaining four are transparent
misses of any kind and never veto six strict passes. Missing/ambiguous evidence at maturity becomes
`skipped_telemetry`; complete evidence below six becomes `skipped_agent_activation`; unresolved
Agent Insights after verified traffic becomes `skipped_insight`. No skipped version starts Insight
or resends traffic. The lane continues after pacing, including all four issues when `v0` is skipped.

Each Agent monitor resets once before its first eligible Insight and never between versions. Every
version persists a timestamp-derived lookback and exact operation set. Prior-version traces,
including skipped versions, are excluded by identity rather than monitor cursor. Composition runs
long package work outside the coordinator lock and publishes `COMPOSED` through one final CAS.

Daily and staging Projects prohibit ad-hoc debug traffic. Monitor reset does not delete telemetry.
Debug locally or in a separately owned sandbox. Qualification evidence is accepted only when exact
run, Agent, provider-version, operation, and invocation-time-window correlation hydrates and remains
stable within the bounded post-invoke deadline.

All potentially long operations emit public-safe progress and heartbeats. Phase artifacts bind the
active lifecycle and cannot be overwritten. `daily-status` and `daily-guide` are read-only and never
fan out, send traffic, or start Agent Insights.

If a run cannot be resumed safely, close it centrally with:

```powershell
python -m agent_insights_quality daily-fail `
  --reason-code <public_safe_code> --confirm
```

This writes a private immutable failure receipt and moves the lifecycle to terminal `FAILED`, allowing
the next Pacific business date to prepare. It never deletes checkpoints, receipts, telemetry, or
provider resources.

An ambiguous Insight start without a provider run ID remains quarantined until read-only
reconciliation proves whether a run exists. A known failed/polling Insight run may be retried only
from the same immutable traffic receipt and never resets the monitor or resends endpoint traffic.

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
for endpoint/telemetry/Agent Insights runtime, 5.3 minutes for per-Agent Sol assessment, and 13 seconds
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

`daily-compose` writes private assessment packages beside `run-manifest.json`. Packages contain
privacy-safe per-request assertion outcomes, full-request trace proof, and separate card-linked trace
proof. Use up to five visible Copilot assessment sub sessions, one per Agent's baseline and four issues,
with GPT-5.6 Sol and `src/agent_insights_quality/prompts/assessment.md`. Then run
`daily-validate-assessments` with all 25 outputs. If an eligible output remains inconclusive, run its
one focused read-only recheck in the same per-Agent assessment lane and pass the exact replacement to
the validation command. Missing, extra, stale, or unbound outputs fail closed.

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

An email-only test publishes no GitHub report by default. When `daily-prepare` also receives
`--publish-preview`, finalization first writes and validates the private sanitized report set, then
uses `gh api` Git Data operations to append `<public-run-id>/` to the dedicated orphan
`aiq-email-test-preview` branch. The branch contains only one manifest, `report.json`, `report.md`,
and exactly five per-Agent Markdown files per run. It is never based on `main`, never creates a pull
request, and never modifies or deletes an existing run directory. Before updating the ref, the
publisher validates the complete existing branch against the managed schema and regenerates every
Markdown file from its public report; unmanaged paths, private links, or divergent content fail
closed. Email links use the permanent branch plus unique run directory, while catalog links continue
to target `main` and the private improvement preview remains unlinked.

The lifecycle enforces this downstream order: assessment validation, focused recheck replacement,
improvement input, one improvement-analysis session, finalization, ADX attempt, immutable email
request, one-time send claim, one provider receipt import, generated-path validation, and exactly one
registered Daily pull request. `daily-email-claim` is the only send authorization. The claim and
provider receipt stay private; reports and the PR contain only sanitized delivery and ADX status, not
a public attestation.

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
  --output $HOME\.aiq-runtime\agent-insights-quality\daily-workflow\runs\<run>\email-receipt.json
```

The coordinator returns the exact canonical private output path. Never copy the provider receipt into
the repository or generated pull request.

## Replay

Read-only replay validates the immutable manifest and retained evidence coordinates:

```powershell
python -m agent_insights_quality replay-run --manifest <private-run-manifest>
```

Replay does not reset monitors, send traffic, mutate Insights, or write telemetry. A true reproduction
is a new run with new endpoint traffic and a new run identity.

## Generated pull requests

Generated branches use `aiq-daily/`. Only new-format report, latest, trend, improvement, and per-Agent
Markdown files are allowed. Email claims and provider receipts remain private. A complete
schema-valid numeric report enables auto-merge after required checks. Incomplete execution creates no
generated branch.

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
