# Agent Insights Daily Quality Platform

## 1. Problem and proposed outcome

Agent Insights can identify real agent failures, but the current quality is not consistently high
enough for a low-noise customer experience. The platform needs a continuous, production-engine test
loop that answers one leadership question every day:

> Did Agent Insights meet the customer quality bar today?

The proposed platform will:

1. Exercise the production Agent Insights engine against controlled synthetic Foundry agents.
2. Test a healthy baseline and a complete catalog of intentionally injected defects every day.
3. Validate every insight field and the insight set as a whole.
4. Use GitHub Copilot automation, pinned to GPT-5.6 Sol, as the semantic quality judge.
5. Maintain durable knowledge of new, known, resolved, and regressed quality gaps.
6. File or reopen high-confidence Azure DevOps bugs without creating duplicates.
7. During qualification, send the concise daily email only through the protected test-recipient
   variable; promote to the protected production-recipient variable after explicit signoff.

The framework should extend the existing Agent Insights benchmark concepts rather than replace them.
The current Vienna benchmark already measures recall, precision, noise, category/severity accuracy,
localization, distinctness, umbrella findings, duplication, meaningfulness, remediation correctness,
grounding, token cost, and latency. The new repository adds a live, daily, production-engine system
test around those concepts.

## 2. Confirmed decisions

| Decision | Choice |
| --- | --- |
| Operational repository | `ninghu/agent-insights-quality` |
| Repository visibility | GitHub **Public** |
| Repository owner/automation account | `ninghu` |
| Public-data boundary | Synthetic data and public-safe contracts only; private resource identifiers are runtime inputs |
| Automation identity | User's personal Microsoft/GitHub identity for Azure, GitHub, ADO, and email |
| Test data | Controlled synthetic agents only; no customer production traces |
| Azure subscription | Authorized subscription supplied at runtime; never stored in the repository |
| Resource group | Dedicated resource group supplied at runtime; never stored in the repository |
| Region | `westus2` |
| Foundry topology | One persistent account; one date-stamped project per daily run |
| Project retention | 7 days |
| Raw run/report artifact retention | 90 days |
| Test domains | Finance, support ticket, healthcare scheduling, travel planning, weather |
| Agent types | Weather and healthcare prompt; finance and travel hosted-code; support ticket hosted custom-container |
| Scenario cadence | Full scenario catalog every day |
| Insight generator | GPT-5.6 Terra through the production Agent Insights API |
| Test-agent model | GPT-5.6 Terra |
| Semantic judge | GitHub Copilot automation pinned to GPT-5.6 Sol |
| Bug verification | Primary Copilot judgment plus an independent second Copilot pass for bug candidates |
| Bug filing threshold | One reproduction, both judgments at least 0.95 confidence, deterministic checks pass, no matching bug |
| Resolved-memory rule | Three consecutive complete clean full-catalog runs |
| Recurrence rule | Mark as regression and reopen the existing ADO bug |
| Quality enforcement | Strict from day one |
| Generated updates | Generated-only PR, auto-merged after path/schema/tests pass |
| Daily repository history | Commit each day's detailed `plan.md` and `report.md` plus machine-readable report data |
| Email rollout | Protected test-recipient variable; protected production-recipient variable after signoff |
| Report/bug language | English |
| Schedule | Run overnight and deliver the report by 08:00 Pacific |

The personal-identity choice is an explicit tradeoff. The implementation must use short-lived,
user-authorized credentials exposed by the GitHub App/runtime and must never store a PAT, Azure
refresh token, ADO token, mail token, or other credential in the repository or artifacts. An
expired or unavailable identity makes the run `INCONCLUSIVE`; it must never produce a success-shaped
report.

## 3. Current Agent Insights alignment

The production engine currently emits these stable categories:

- `tool_call_failures`
- `latency`
- `cost_tokens`
- `reliability_errors`
- `hallucinations`
- `output_quality`
- `context_memory`
- `safety_guardrails`

The quality platform will keep these as the expected output taxonomy. Test scenarios are deliberately
more granular. For example, wrong tool, missing tool, incorrect arguments, silent tool failure, and
failed recovery are separate scenario contracts even when several correctly map to
`tool_call_failures`.

Existing benchmark assets may be reused conceptually for ground-truth, scorecard, semantic mapping,
scenario, and model-qualification contracts. This repository will not copy or modify the production
engine or publish private source locations.

The public quality repository invokes an authorized deployed Agent Insights surface and records only
public-safe build labels, model labels, prompt hashes, catalog versions, and agent digests. Internal
endpoints and resource identifiers are supplied at runtime and never committed.

## 4. High-level architecture

```text
GitHub Copilot scheduled automation (GPT-5.6 Sol, `ninghu`)
  |
  +-- deterministic Python orchestrator
  |     |
  |     +-- fail-closed `run-daily` plan/deploy/resume boundary
  |     +-- Azure deployment/bootstrap (Bicep + Foundry SDK/REST)
  |     +-- daily project and agent-version lifecycle
  |     +-- traffic generation and trace-ingestion checks
  |     +-- production Agent Insights monitor/run APIs
  |     +-- structural validators and scorecard calculation
  |     +-- ADO query/create/update client
  |     +-- detailed report and LT email renderer
  |
  +-- semantic judging of bounded evidence bundles
  |     +-- primary field-level and collection-level judgment
  |     +-- independent, blinded verification for bug candidates
  |
  +-- GitHub/ADO/email actions through the user's connected automation tools
  |     +-- generated-only GitHub PR and auto-merge
  |     +-- ADO bug create/update/reopen
  |     +-- direct HTML email send, including failure reports
  |
  +-- repository history
        +-- quality memory
        +-- detailed daily plan and report Markdown
        +-- latest status, trend data, and generated inventories
```

Operational dependencies:

- Persistent Foundry account supplied through private runtime configuration.
- Shared Log Analytics/Application Insights connection.
- Shared Azure Container Registry for hosted-container images.
- Shared storage account/container for 90-day raw run artifacts.
- GPT-5.6 Terra deployments for test-agent traffic and insight generation.
- A GitHub Copilot automation environment with the user's Microsoft mail capability connected.
- Authorized Azure DevOps access to read a privately configured bug template, search work items,
  create bugs, add evidence, and reopen resolved bugs.

## 5. Public repository

Repository: `ninghu/agent-insights-quality`, with **Public** visibility. `ninghu` is the initial
owner/maintainer and the identity used by automation. Repository content is restricted to synthetic
data and public-safe contracts; private Azure, Agent Insights, and ADO identifiers are runtime inputs.

```text
.
|-- .github/
|   |-- copilot/
|   |   `-- daily-bootstrap-prompt.md
|   |-- skills/
|   |   |-- agent-insights-quality-daily/
|   |   |-- onboard-test-agent/
|   |   |-- onboard-test-scenario/
|   |   `-- replay-quality-run/
|   `-- workflows/
|       `-- validate-generated-change.yml
|-- infra/
|   |-- main.bicep
|   `-- modules/
|-- config/
|   `-- reporting.yaml
|-- agents/
|   |-- weather-prompt/
|   |-- healthcare-scheduling-prompt/
|   |-- finance-hosted/
|   |-- travel-hosted/
|   `-- support-ticket-hosted-image/
|-- scenarios/
|   |-- catalog.yaml
|   |-- mutations/
|   `-- traffic/
|-- schemas/
|   |-- daily-plan.schema.json
|   |-- daily-status.schema.json
|   |-- evidence-bundle.schema.json
|   |-- judgment.schema.json
|   |-- scorecard.schema.json
|   `-- quality-memory.schema.json
|-- src/agent_insights_quality/
|   |-- planning/
|   |-- provisioning/
|   |-- deployment/
|   |-- traffic/
|   |-- insights/
|   |-- validation/
|   |-- memory/
|   |-- ado/
|   |-- reporting/
|   `-- cleanup/
|-- state/
|   |-- quality-memory.json
|   `-- QUALITY_MEMORY.md
|-- reports/
|   |-- latest.json
|   |-- latest.md
|   |-- trend.json
|   `-- daily/
|       `-- YYYY/MM/DD/
|           |-- plan.md
|           |-- plan.json
|           |-- daily-status.json
|           |-- report.md
|           `-- report.json
|-- docs/
|   |-- TEST_AGENTS.md
|   |-- SCENARIO_CATALOG.md
|   |-- QUALITY_RUBRIC.md
|   `-- OPERATIONS.md
`-- tests/
```

Repository rules:

- Healthy agent baselines are immutable source-of-truth implementations.
- Injected versions are generated from explicit mutation manifests into temporary build directories;
  automation never edits a healthy baseline in place.
- Agent and scenario manifests are authoritative. `TEST_AGENTS.md` and `SCENARIO_CATALOG.md` are
  generated and verified in CI, so adding an agent or scenario automatically updates documentation.
- Daily automation may modify only generated report/state paths. A path allowlist prevents it from
  silently changing baselines, scenarios, infrastructure, or judging policy.
- `config/reporting.yaml` starts in `test` mode and names only protected recipient variables. Changing
  to production mode requires a human-reviewed PR; daily automation cannot promote its own audience.
- Raw traces, tokens, credentials, complete prompt definitions, and ADO private content are never
  committed. Raw synthetic run artifacts live in Azure Blob Storage for 90 days.
- Every day's detailed plan and report are committed as sanitized Markdown, with companion JSON for
  automation. These public files are long-lived repository history, so they contain opaque hashes,
  counts, metrics, verdicts, and synthetic evidence summaries, but no private links, resource IDs,
  raw trace payloads, or credentials.
- The repository landing page makes onboarding discoverable: how to add a test agent, add a scenario,
  replay a run, interpret metrics, and find the current Agent Insights pages.

## 6. Azure resource design

### Persistent resources

Deploy once through Bicep into the privately configured dedicated resource group in `westus2`:

- Foundry account with a globally unique generated suffix.
- Log Analytics workspace and Application Insights.
- Azure Container Registry.
- Storage account and private report-artifact container with a 90-day lifecycle rule.
- Two named GPT-5.6 Terra deployments backed by the same model:
  - `terra-test-agents` for agent traffic.
  - `terra-insights-generator` for Agent Insights runs.

Separate deployment names make usage and throttling attribution clear even if quota is shared.
GitHub Copilot supplies GPT-5.6 Sol, so no Sol deployment is needed in the Foundry account. Email uses
one digest-bound direct-mail handoff: connected Copilot mail first, Graph only with confirmed
`Mail.Send`, then local Outlook COM only on `hostId=local` for the verified authenticated-user test
mailbox with Sent Items verification. The workflow stops after the first confirmed success. No Logic
App or mail relay resource is provisioned.

### Daily project

Create one project named `aiq-YYYYMMDD` using the Pacific report date. A rerun uses
`aiq-YYYYMMDD-rNN`. Apply tags:

- `purpose=agent-insights-quality`
- `reportDate=YYYY-MM-DD`
- `expiresOn=YYYY-MM-DD`
- `automationOwner=<user alias>`
- `catalogVersion=<hash>`

Connect the project to the shared Application Insights resource and model deployments. Cleanup deletes
expired daily projects after seven days. It must resolve exact resource IDs and delete only resources
tagged for this framework.

### Infrastructure preflight

Every daily run verifies:

- Correct tenant and exact authorized subscription, both resolved from private runtime configuration.
- Existing resource group location and expected tags.
- Required Foundry, Logs, model-inference, ACR, storage, ADO, GitHub, and email permissions.
- Production Agent Insights endpoint availability.
- GPT-5.6 Terra deployment availability and quota.
- Connected Microsoft mail capability and the fixed internal recipient allowlist.

No preflight failure is downgraded to a quality pass.

## 7. Five healthy test agents

Every test agent has a stable framework ID. The lower-case ID is the required prefix of the Foundry
agent name, so links, traces, reports, plans, and bugs can be correlated without guessing.

| Stable ID / required name prefix | Domain | Agent type | Healthy responsibility | Representative tools |
| --- | --- | --- | --- | --- |
| `aiq-001-weather` | Weather | Prompt agent | Resolve location and provide grounded current/forecast data efficiently | Geocode, current weather, forecast |
| `aiq-002-healthcare` | Healthcare scheduling | Prompt agent | Find synthetic availability and schedule only with explicit confirmation; no diagnosis or real patient data | Provider lookup, slot search, appointment create/cancel |
| `aiq-003-finance` | Finance | Hosted-code agent | Explain synthetic account activity and prepare a bounded budget summary; never execute unauthorized transfers | Account lookup, transaction search, budget calculation |
| `aiq-004-travel` | Travel planning | Hosted-code agent | Build a constrained itinerary without fabricating inventory or booking without confirmation | Flight/hotel search, itinerary, booking |
| `aiq-005-ticket` | Support ticket | Hosted custom-container agent | Triage, enrich, update, and escalate synthetic tickets with current-ticket grounding | Ticket read/update, customer context, escalation |

Healthy-agent requirements:

- Use only generated synthetic data.
- Naturally emit complete OpenTelemetry traces from endpoint execution, with stable agent name,
  immutable version, prompt/source/image digest, model, tool definitions, tool arguments/results,
  parent-child links, and final response.
- Generate every test trace by invoking the deployed agent endpoint with a real synthetic user
  request. The framework must never write, inject, or synthesize trace rows directly in Application
  Insights. Application Insights is read only to confirm naturally emitted endpoint traffic and to
  obtain the evidence consumed by the production engine.
- Have deterministic traffic seeds and expected task outcomes.
- Include normal variance such as slow-but-valid model calls and recoverable tool errors so healthy
  behavior is not mistaken for a defect.
- Pass task completion, task adherence, tool selection, tool-input, tool-output-utilization,
  groundedness, safety, and efficiency checks before becoming a baseline.
- Produce zero Agent Insights under the healthy baseline traffic.

## 8. Scenario catalog

### Catalog contract

Each scenario is an immutable, reviewable record containing:

- Stable scenario ID and version.
- Priority and customer impact.
- Applicable agent domains/types.
- Mutation mechanism and exact healthy-to-fault delta.
- Traffic recipe and deterministic random seed inputs.
- Expected root cause, engine category, severity, and remediation boundary.
- Expected trace IDs/spans/tool calls and minimum evidence count.
- Negative controls that must not be cited.
- Expected behavior across agent versions.
- Conflict tags used by the planner to avoid ambiguous co-location.
- Retirement/replacement metadata.

### Initial full-catalog coverage

The detailed scenario taxonomy is intentionally richer than the eight engine categories.

| Scenario family | Required cases | Expected engine category |
| --- | --- | --- |
| Grounding and correctness | Hallucination; contradiction of successful tool output; fabricated inventory after sub-agent/tool failure | `hallucinations` |
| System instruction adherence | System-prompt drift; ignored user correction; explicit output-schema violation | `context_memory` or `output_quality` based on first broken mechanism |
| Tool selection | Wrong tool; missing required tool; redundant/unnecessary tool | `tool_call_failures` |
| Tool arguments | Missing required argument; wrong type/format; wrong entity/version/scope | `tool_call_failures` |
| Tool-result handling | Silent tool error; ignored partial failure; malformed output accepted; tool result not used | `tool_call_failures` |
| Recovery | No retry where retry is required; unbounded identical retry; bad fallback; failed escalation | `tool_call_failures` or `reliability_errors` |
| Planning | Flawed step order; incomplete plan; missing owner/validation; task evasion/no-op | `output_quality`, `tool_call_failures`, or `reliability_errors` |
| Capability awareness | Claims or proposes unavailable tools/integrations; attempts unsupported action | `output_quality` or `tool_call_failures` |
| Context and memory | Stale entity substitution; dropped standing constraint; cross-user/entity contamination | `context_memory` |
| Context size and cost | Context explosion; repeated history/tool payload; over-fetch; excessive verbosity | `cost_tokens` |
| Response completion | Response truncation; incomplete multi-part task; omitted required fields | `output_quality` |
| Safety and authorization | Guardrail bypass; action without confirmation; malformed approval; synthetic cross-account PII leak | `safety_guardrails` |
| Latency and loops | Sequential redundant calls; avoidable waits; agent loop inefficiency | `latency` or `cost_tokens` |
| Runtime reliability | Pre-model abort; post-tool orchestration abort; model failure; success reported for no-op | `reliability_errors` |
| Multi-agent behavior | Bad handoff; lost constraints; fabricated synthesis after child failure | Category follows the first broken mechanism |
| Trace interpretation | Parent-child span correlation; outer-span zero-token false positive; handled child failure | Negative/control or matching reliability case |
| Insight lifecycle | Cross-version stale finding; same root cause repeated across windows; fixed issue recurrence | Existing category with version-aware identity |
| Collection quality | Duplicate cards for one root cause; umbrella card merging distinct root causes; symptom/root-cause fragmentation | Collection-level invariant |
| Healthy controls | Fully healthy; expected model latency; handled transient failure; ordinary token use | No insight |

The first catalog release should port applicable cases from Vienna's existing synthetic and reviewed
Prompt/Hosted suites, then add missing production-lifecycle and attribute-quality cases. The catalog
is versioned; changing ground truth requires human review and never happens automatically after a
failed run.

## 9. Daily test-plan generation

The planner treats the full catalog as a predefined reviewed issue library. Each weekday plan
includes all six healthy controls, nine single-root P0 faults, the two-root umbrella P0 collection
probe on Monday/Wednesday/Friday, and one deterministic partition of the 47 P1/P2 faults. The
Monday-Friday `9/10/9/10/9` horizon yields expected totals of `20/19/20/19/20` under the expected
cap of four per agent:

1. Compute a reproducible seed from `report date + catalog hash + selection-policy hash`.
2. Assign every selected scenario exactly once to a compatible agent and version wave; fail rather
   than silently dropping a scenario.
3. Balance scenarios across the five domains and three implementation types.
4. Put no more than four distinct injected root causes on an agent across all daily versions and in
   one insight run so failures remain isolated and diagnosable. This planning constraint is not an
   output-count quality gate.
5. Never co-locate conflict-tagged scenarios whose traces would make ground truth ambiguous.
6. Maximize daily engine-category coverage without repeating a rotating scenario. P0 supplies
   context, reliability, and safety daily; rotating inventory supports tool/output on five days,
   cost/hallucinations on four, and latency on three.
7. Create additional immutable versions when an agent cannot safely hold all assigned scenarios in
   one version.
8. Write `plan.json` before deployment, including every assignment, version digest, traffic
   seed, expected run window, and expected finding.

The randomization is reproducible. A failed run can be replayed exactly from the plan and artifact
manifest.

### Daily repository artifacts

Each date has a reviewable, permanent, sanitized record:

- `reports/daily/YYYY/MM/DD/plan.md`: readable execution plan with seed, catalog/engine versions,
  agent IDs and names, scenario-to-agent/version assignments, deployment waves, endpoint traffic
  counts, expected findings, and expected no-insight controls.
- `reports/daily/YYYY/MM/DD/plan.json`: schema-validated authority consumed by the orchestrator.
- `reports/daily/YYYY/MM/DD/daily-status.json`: public-safe evidence-complete handoff that enumerates
  bounded primary judgment targets and the ordered Copilot-owned completion stages without exposing
  private artifact coordinates.
- `reports/daily/YYYY/MM/DD/report.md`: detailed engineering report with the complete numeric
  scorecard, per-agent/per-version/per-scenario results, field-level judgments, duplicate/umbrella
  analysis, opaque trace and Agent Insights references, memory changes, bug actions, costs/latency,
  and retained diagnostics.
- `reports/daily/YYYY/MM/DD/report.json`: canonical machine-readable report used to render Markdown,
  LT email, trends, and future comparisons.
- Reruns use `reports/daily/YYYY/MM/DD/aiq-YYYYMMDD-rNN/` with the same artifact set so each
  rerun preserves its full plan identity and cannot overwrite the original daily artifacts.

The LT email links to `report.md` for readers who want the full numbers.
The canonical report also carries scorecard-derived quality-bar thresholds/actuals and one immutable
daily-plan-bound validation checklist per agent. The Markdown expands each checklist as a per-version
one-pager. The existing four-section email summarizes the same expected roots, categories, severities,
observed cards, and review focus so a reviewer can understand the result before opening private links.

## 10. Daily execution flow

### Phase A: Planning, provisioning, and preflight

1. Resolve the Pacific report date, then generate or byte-validate the immutable weekday plan,
   including an explicit `rNN` suffix for reruns.
2. Validate protected runtime configuration and identity only after the plan exists.
3. Deploy the reviewed qualification-project Bicep with the exact `plan.project.name`, report date,
   expiry, catalog hash, and `connectionNameSuffix=plan.project.name`. Pass only protected resource
   names/IDs; ARM resolves the Application Insights connection string server-side.
4. Persist the successful deployment, then wait a bounded 15 minutes for project-managed-identity and
   ACR data-plane authorization propagation. Do not infer readiness from role-assignment listings.
5. Validate the exact project, connections, endpoints, model deployments, quota, and report/bug
   integrations, then execute or resume the live adapter from durable private state.
6. Bound runtime execution to four hours. An ordinary work failure stops only that agent's sequence;
   independent agents finish and one aggregate `INCONCLUSIVE` result retains their safe failure codes,
   opaque work references, deployments, and receipts. Resume skips complete agents/versions and
   retries only failed or unstarted work. Only explicit operator abort performs exact-resource
   cancellation and cleanup, after which the attempt is non-resumable and needs a rerun suffix.

### Phase B: Healthy baseline

1. Deploy the five immutable healthy versions in parallel.
2. Call each deployed agent endpoint with deterministic healthy requests in parallel.
3. Wait until all expected trace IDs are queryable in Application Insights.
4. Create monitors and trigger on-demand production Agent Insights runs.
5. Require each run to complete successfully and return zero insights.

A healthy false positive immediately means `NOT AT BAR`, but the workflow continues through the
faulted catalog when infrastructure is still usable so the report preserves the day's diagnostic
value.

### Phase C: Fault-injection waves

For each planned wave:

1. Materialize the version from the healthy baseline plus declared mutation manifests.
2. Deploy immutable prompt/source/image versions and record exact hashes/digests.
3. Call the deployed agent endpoint with random-but-seeded synthetic requests, including the required
   recurrence volume and healthy decoys. Never emit traces directly.
4. Verify trace completeness and capture the exact half-open analysis window.
5. Trigger an on-demand Agent Insights run with GPT-5.6 Terra.
6. Poll to a terminal state, retrieve full insight details, and store a sanitized evidence bundle.
7. For later versions of the same agent, ensure windows do not overlap and retain prior-insight state
   so cross-version deduplication, stale findings, and recurrence are tested.

Different agents run concurrently. Versions of the same agent run sequentially because version
lineage, monitor checkpoints, and previous-insight reconciliation are part of the test.
Hosted-code and custom-container recovery, creation, and activation polling additionally share one
process-wide gate to avoid cross-hosted deployment contention. Prompt deployment can remain parallel,
and all endpoint traffic resumes normal cross-agent concurrency after deployment.
An active hosted version must create and remove a validation session bound to its exact version
before it is accepted for traffic.
Prompt and hosted session requests retry bounded pre-response HTTP 408, 429, and 5xx failures with
`Retry-After` or conservative exponential backoff. Durable per-fixture receipts prevent replay of
completed traffic on outer resume. Nontransient 400s and failures with a response ID fail immediately,
and no response body is promoted into public state.
Each scenario envelope carries the selected agent's reviewed customer-like synthetic domain input
and expected tool contract. The scenario ID, provenance, correlation, and bounded recipe marker are
additive metadata rather than a replacement generic prompt.
Zero-finding prompt assignments validate the exact tool sequence and require a grounded textual
answer after every tool result. Faulted assignments relax those checks only when needed to preserve
the injected failure for Agent Insights.

### Phase D: Validation and judgment

1. Run deterministic structural checks.
2. Build bounded, schema-validated semantic evidence bundles.
3. Have the scheduled GitHub Copilot session judge each bundle using the pinned GPT-5.6 Sol model.
4. Validate the judge's strict JSON output and calculate the scorecard.
5. Run an independent, blinded Copilot verification for each automatic-bug candidate.

After evidence completion, `run-daily` validates every selected scenario's final evidence reference,
writes bounded primary packages in private state, and emits `daily-status.json`. That handoff names
only existing CLI contracts and permits verifier work only after primary confidence, deterministic,
provenance, reproducibility, and Agent Insights ownership gates establish an eligible candidate.

### Phase E: Triage, memory, bugs, and report

1. Reconcile today's findings against the durable quality memory.
2. Search ADO for exact fingerprint matches and likely semantic duplicates.
3. Record ADO candidates only while the reviewed policy keeps auto-apply false; no write request is
   issued.
4. Generate the detailed English daily `plan.md`, `report.md`, JSON records, and the simpler
   narrative LT email HTML.
5. Upload raw/sanitized artifacts to the 90-day storage location.
6. Open a generated-only PR for memory, latest status, trend data, generated inventories, and the
   date-stamped plan/report history.
7. Auto-merge only after schema, rendering, link, path-allowlist, and consistency checks pass.
8. Submit the report through the no-duplicate direct-mail handoff to the exact allowed-domain
   recipient resolved from `config/reporting.yaml`; qualification starts with the authenticated-user
   test mailbox or protected test-recipient variable. Preserve one content digest and import an
   ordered receipt before claiming delivery.
9. Delete expired daily projects and artifacts using exact tags and retention dates.

The workflow has a finalizer that always attempts an email. If the automation fails before a normal
report is complete, it sends an `INCONCLUSIVE` failure report to the same configured recipient with the failed
phase, last confirmed stage, plain-language reason, affected agents, retained diagnostics link, and
safe next action. Never advance clean streaks, resolve memory issues, or close/reopen bugs from an
incomplete run. Try connected Copilot mail first; use Graph only when `Mail.Send` is authorized; use
local Outlook only on `hostId=local` for the verified authenticated-user test mailbox and verify Sent
Items. Stop after the first confirmed success and never use a Logic App. If every authorized
transport is unavailable, retry transient failures with bounded backoff, preserve the rendered
failure email, mark the automation failed, and surface delivery failure; no implementation can
truthfully guarantee email when every authorized channel is down.

## 11. Insight validation contract

### Deterministic checks

For every insight:

- Required `title`, `description`, `category`, `severity`, `trace_count`, linked traces, and
  `proposed_fix` fields are present and valid.
- Category and severity use the public contract values.
- Every linked trace belongs to the correct project, agent, immutable version, and analysis window.
- `trace_count` and sampled trace links are internally consistent.
- Proposed-fix kind and artifact shape are valid for prompt, hosted-code, or prose fallback.
- Proposed changes never reference a tool/capability absent from the deployed agent.
- Text contains no credentials, secrets, or synthetic PII values.
- The observed count for each report date, run, and agent equals the sum of
  `expected.finding_count` across its selected plan assignments.
- Exact IDs/signatures/evidence fingerprints do not duplicate another insight.

For the collection:

- Healthy baseline count is exactly zero.
- Every expected scenario maps to at most one insight.
- Every insight maps to at most one independently fixable root cause.
- No current insight duplicates a previous active insight for the same root cause.
- Distinct root causes are not collapsed into one umbrella card.
- Later-version findings do not incorrectly reuse stale version evidence.

### Copilot semantic judgment

Each evidence bundle contains only the information needed to judge:

- Daily-plan ground truth and expected failure contract.
- Healthy baseline and mutation diff.
- Agent instructions, available tools, and the current `version_sequence` phase/digest mapping.
- User requests, tool calls/results, final responses, and trace hierarchy.
- Produced insight fields and previous matching insight metadata, including its separately validated
  prior phase, run, and immutable version digest.

Trace and tool content is explicitly fenced as untrusted evidence. Copilot must return strict JSON
with:

- Ground-truth scenario to insight mapping.
- `correct`, `partially_useful`, or `incorrect_noise` verdict.
- Root-cause accuracy.
- Title specificity and fidelity.
- Description accuracy and evidence support.
- Proposed-fix actionability, feasibility, and ownership.
- Category correctness.
- Severity appropriateness.
- Linked-trace precision and sufficiency.
- Customer meaningfulness.
- Duplicate, fragmentation, and umbrella relationships.
- Confidence and concise reasoning.

An insight counts as a true positive only when it maps to one expected root cause and every required
attribute passes. A partially useful card is visible in the report but does not count as a full true
positive.

The primary judge prompt hash, Copilot model ID, evidence schema version, and output hash are recorded.
For bug candidates, the second Copilot pass receives the evidence bundle without the primary verdict
or reasoning. Both passes must independently identify the same product defect with confidence at
least 0.95.

## 12. Metrics and strict quality bar

### Core metrics

- Scenario detection recall, including High/Medium/Low recall.
- Insight precision and false-positive/noise rate.
- F1.
- Healthy-baseline false-positive rate.
- Category and severity accuracy.
- Title, description, proposed-fix, and linked-trace pass rates.
- Evidence localization.
- Meaningfulness and actionability.
- Distinctness.
- Duplication and fragmentation rate.
- Umbrella rate.
- Cross-version stale/overlap rate.
- Expected and observed insight count per run and agent, plus count-mismatch noise/misses.
- Engine/judge/trace structural failure count.
- Engine latency, model calls, and tokens as non-quality efficiency diagnostics.
- New, known, resolved, and regressed issue counts.

### Daily verdict

`AT BAR` requires all of the following:

- The complete reviewed daily selection completed.
- Every healthy baseline run produced zero insights.
- Every run and agent produced exactly its plan-assignment expected finding count.
- High-severity detection recall is 100%.
- Overall scenario recall is at least 90%.
- Insight precision is at least 95%.
- Category, severity, title, description, proposed-fix, and linked-trace correctness are 100% among
  accepted true positives.
- Duplication rate is 0%.
- Umbrella rate is 0%.
- Cross-version stale/overlap rate is 0%.
- No structural, provenance, secret/PII, or judge-schema failure occurred.
- No unresolved low-confidence judgment prevents classification.

`NOT AT BAR` means the complete run produced evidence that any quality gate failed.

`INCONCLUSIVE` means infrastructure, identity, quota, trace ingestion, production API, Copilot
judging, or report consistency prevented a complete trustworthy result. `INCONCLUSIVE` is never
presented as a pass.

## 13. Durable quality memory

`state/quality-memory.json` is the machine-readable source of truth.
`state/QUALITY_MEMORY.md` is generated from it.

Each issue record contains:

- Stable fingerprint based on root-cause contract, affected engine surface, and validation target.
- Human title and concise description.
- State: `new`, `known`, `improving`, `resolved`, or `regressed`.
- First seen, last seen, occurrence count, and consecutive clean complete-run count.
- Affected scenarios, domains, agent types, engine build/model, and judge prompt version.
- Last primary/verifier confidence and evidence artifact links.
- ADO bug ID, URL, state, and last synchronization result.
- Resolution evidence and regression history.

State rules:

- A newly confirmed gap becomes `new`.
- A repeated matching gap becomes `known`.
- A complete clean run increments its clean streak.
- Three consecutive complete clean full-catalog runs mark it `resolved`.
- Incomplete runs do not increment or reset the clean streak.
- Any confirmed recurrence changes `resolved` to `regressed` and resets the streak.
- Automation does not delete history or rewrite fingerprints.

New agents and scenarios update their source manifests; generated catalog documents update
automatically. Newly discovered product gaps update quality memory, but automation does not silently
invent or promote new benchmark ground truth. A proposed new scenario is recorded as a candidate for
human review.

## 14. Azure DevOps bug automation

`config/ado-policy.yaml` is the public, reviewed authority. Candidate reporting starts enabled and
automatic apply starts disabled. The runtime `AIQ_ADO_AUTO_APPLY_ENABLED` switch may further disable
an approved apply setting, but cannot override false to true. Generated automation cannot modify
configuration; enabling writes requires a normal human-reviewed config change. With apply disabled,
all create, patch/update, reopen, and comment/evidence paths return an explicit candidate-only result
and issue zero write HTTP requests. Template lookup, work-item reads, and WIQL duplicate search remain
available for side-effect-free planning.

### Template and duplicate handling

At bootstrap, read the privately configured bug template through ADO APIs and cache only its
non-secret field contract. The organization, project, template ID, owner ID, and endpoint are private
runtime configuration and must never be hard-coded or committed.

Before creating a bug:

1. Fetch the privately configured bug template at runtime and honor its priority, severity, value
   area, repro steps, area/iteration, state/reason, tags-add, and title-prefix fields without
   committing internal values or paths.
2. Search quality memory by exact fingerprint.
3. Query ADO bugs across every state, including New, In Review, Resolved, Done, and Removed, using the
   fingerprint marker, `AgentInsights` and relevant `Quality` tags, title tokens, and root-cause
   terms. Compare both exact and semantic duplicates.
4. If an active match exists, add a new occurrence comment and link it in the report.
5. If a resolved match exists, reopen it, add fresh evidence, tag it as a regression, and link it in
   the report.
6. Create a bug only when no matching work item exists.

Semantic duplicate search includes existing coverage for duplicate insights, prompt/context
grounding, latency baselines, version lifecycle, unavailable-tool fixes, and no-findings explanations.

### Automatic bug gate

All conditions are mandatory:

- One complete reproducible daily occurrence.
- The defect belongs to Agent Insights, not the injected agent, test framework, or transient
  infrastructure.
- Primary and blinded verifier agree on the same root cause.
- Both confidence values are at least 0.95.
- Deterministic contract and provenance checks pass.
- Exact reproduction artifacts are retained and linked.
- Duplicate search completed successfully.

If any condition is missing, record a candidate in memory and the report without creating a bug.

### Bug content

Populate the template with:

- Customer impact and why the insight is noise, misleading, duplicated, missed, or unactionable.
- Exact report date, run ID, production engine/build, generator model, project, agent domain/type,
  immutable version, scenario ID, and traffic seed.
- Expected insight behavior versus actual behavior.
- Field-by-field assessment of title, description, fix, severity, category, and linked traces.
- Minimal deterministic reproduction steps.
- Relevant synthetic trace IDs and a 90-day artifact link.
- A direct link to the affected test agent's Agent Insights page in the date-stamped Foundry project.
  This link is written only to the private ADO work item and direct email, not the public repository.
- Root-cause fingerprint marker.
- Primary and verifier confidence.
- Acceptance criteria and the exact catalog scenario that should pass.
- `quality`, `Agent Insights`, and automation/regression tags as applicable.

No raw token, credential, complete secret-bearing payload, or real customer data is included.

The normal-agent page is resolved at runtime as
`https://ai.azure.com/nextgen/r/{sub},{rg},,{account},{project}/build/agents/{urlencodedAgent}/insights`
when the standalone-tab flight is on, with `/monitor/insights` as the fallback suffix. Trace links use
`/build/agents/{urlencodedAgent}/traces/{operation_Id}`. Invocation and response IDs are not trace
IDs; they must be correlated through read-only Application Insights evidence to `operation_Id`.
There are no monitor, run, or insight-ID deep links; selection of an insight is router state only.
Private link values are never committed.

## 15. Daily email

Use the attached Foundry Insights Assessment as the visual and editorial reference, but keep the daily
email shorter and more executive-friendly. The detailed numeric evidence belongs in the committed
`reports/daily/YYYY/MM/DD/report.md`; the email favors clear written conclusions and only simple,
easy-to-interpret numbers. Its narrative states `Expected X findings; observed Y`, calls extras noise,
and calls missing findings missed issues while preserving the four-section navy Outlook layout.

### 1. Summary

- `AT BAR`, `NOT AT BAR`, or `INCONCLUSIVE` banner.
- A short written assessment of whether customer-facing insight quality met the bar and why.
- Report date, production engine/build, and completion status.
- Only approachable headline numbers, such as scenarios completed, correct/partially useful/incorrect
  findings, and new/regressed gaps.
- A 14-day quality trend chart showing daily `AT BAR`, `NOT AT BAR`, and `INCONCLUSIVE` status plus
  trusted-insight rate. Render it as email-compatible table-based bars/status cells, not a
  remote-authenticated image that Outlook cannot load.
- Bugs created, updated, or reopened, phrased as an executive signal rather than a metric dump.

### 2. What we are doing well

- Correctly detected high-value failure modes.
- Strong field-level insight examples.
- Healthy agents correctly producing no insights.
- Improvements and newly resolved gaps.

### 3. Gaps and regressions

- Missed scenarios.
- Incorrect, partially useful, noisy, duplicated, fragmented, or umbrella insights.
- Incorrect title/description/fix/severity/category/trace linkage.
- Cross-version or lifecycle regressions.
- ADO bug links, owners/state when available, and the next validation target.

### 4. Test agents and Agent Insights links

Include one compact table with every test agent:

| Agent ID | Test agent | Type | Agent Insights page | Human validation recommended |
| --- | --- | --- | --- | --- |

The Agent Insights page is the runtime-resolved direct link for the current date-stamped project and
agent and appears only in direct email and private ADO actions. Public committed reports retain an
opaque reference. The last email column contains a short reason and the insight title/link when human
review would add value; otherwise it is exactly `N/A`. Recommend human validation for ambiguous/
partially useful output, primary/verifier disagreement, confidence below the auto-bug threshold,
novel failure contracts, or a proposed fix that cannot be validated deterministically.

Subject format:

`[Agent Insights Quality] <AT BAR|NOT AT BAR|INCONCLUSIVE> - YYYY-MM-DD - <short signal>`

Generate one canonical report model, then render detailed JSON/Markdown and the simpler email-safe
HTML from it. All dynamic content is HTML-encoded. Add deterministic consistency tests so the
subject, banner, narrative, trend, bug links, agent links, and detailed report cannot contradict one
another. The automation follows the digest-bound direct-mail transport order and stops after one
confirmed success. The initial recipient is the authenticated user's test mailbox or a protected test
variable; after email content and delivery are approved, a human-reviewed configuration PR selects
the protected production variable.

## 16. Repository skills and GitHub Copilot bootstrap prompt

The bootstrap prompt stays intentionally short. The workflow authority lives in the repository so
teammates can review and evolve it through normal code review.

Required repository skills:

- `agent-insights-quality-daily`: complete deterministic runbook, Copilot judging contract, failure
  finalizer, memory/ADO/report rules, and allowed mutations.
- `onboard-test-agent`: scaffold a new stable prefixed agent ID, implementation, endpoint traffic
  driver, healthy contract, scenarios compatibility, docs, and tests; reject duplicate IDs/names.
- `onboard-test-scenario`: scaffold a reviewed scenario contract, mutation, endpoint traffic recipe,
  expected evidence/category/severity/fix, controls, compatibility/conflict tags, docs, and tests.
- `replay-quality-run`: reproduce a date/plan hash safely without changing ground truth or memory
  unless explicitly promoted through review.

Store this minimal bootstrap prompt in `.github/copilot/daily-bootstrap-prompt.md`:

```text
Run today's Agent Insights quality workflow in ninghu/agent-insights-quality.
Follow `.github/skills/agent-insights-quality-daily/SKILL.md` exactly, using the `ninghu`
identity and GPT-5.6 Sol. Always send the final email to the repository-configured recipient; if the
workflow fails, send an INCONCLUSIVE email explaining the failure and retained diagnostics.
```

The automation schedule, model pin, repository, identity, and permissions are configured in the
GitHub Copilot automation itself, not repeated as operational timing instructions in the prompt.

## 17. Implementation workstreams

1. **Repository and contracts**
   - Create the public-safe repository, Python package, schemas, generated-path policy, branch protection,
     CI, and documentation generators.

2. **Infrastructure bootstrap**
   - Add Bicep for persistent resources, scoped cleanup, personal-identity preflight, Terra
     deployments, App Insights, ACR, and storage lifecycle.

3. **Healthy agent suite**
   - Implement and validate the two prompt, two hosted-code, and one hosted-container baseline agents
     plus deterministic synthetic data and traffic.

4. **Scenario catalog and mutation system**
   - Port the useful existing benchmark contracts, add missing failure/lifecycle/control cases, define
     compatibility/conflict metadata, and implement reproducible full-catalog planning.

5. **Production-engine orchestrator**
   - Implement project/agent/version deployment, traffic, trace-ingestion polling, monitor/run API
     calls, non-overlapping windows, artifact capture, retries, cancellation, and safe cleanup.

6. **Deterministic validation and scoring**
   - Implement schema/provenance/count/trace/dedup/version checks and metrics aligned with the existing
     Vienna scorecard.

7. **Copilot judging**
   - Add bounded evidence projection, strict primary and blinded-verifier prompts, JSON validation,
     confidence handling, prompt/model hashing, and failure behavior.

8. **Memory and regression lifecycle**
   - Implement fingerprinting, issue-state transitions, three-clean-run resolution, regression
     detection, and generated Markdown.

9. **ADO bug synchronization**
   - Read the supplied bug template, query active/resolved bugs, enforce the automatic filing gate,
     create/update/reopen work items, and attach reproducible evidence links.

10. **Reporting and email**
    - Implement the canonical report model, detailed daily plan/report Markdown, narrative four-section
      LT email, 14-day trend chart, agent/Insights-link table, consistency/security tests, direct
      Copilot mail delivery, test-to-team recipient promotion guard, and failure-finalizer behavior.

11. **GitHub App automation**
    - Install the minimal bootstrap prompt and repository skills, configure the `ninghu`
      GPT-5.6 Sol workflow, generated-only PR creation, checks, auto-merge, failure email, and audit
      trail.

12. **End-to-end qualification**
    - Run a complete dry run without side effects, a full live run with bug creation disabled, a
      duplicate/reopen simulation, an email-render/delivery test, and finally one fully enabled daily
      run.

The implementation through workstream 11 is present behind `config/runtime-readiness.yaml`. Live
qualification, schedule enablement, recipient promotion, and ADO writes remain separate
human-reviewed operational decisions.

Workstreams 2, 3, 7, and 10 can proceed in parallel after repository contracts are fixed. The live
orchestrator, memory, ADO, and final automation depend on those contracts.

## 18. Validation strategy

- Unit tests for planner constraints, fingerprints, score formulas, memory transitions, ADO
  deduplication, HTML encoding, and cleanup filters.
- Schema/golden tests for every manifest, plan, evidence bundle, judgment, scorecard, memory record,
  bug body, and report.
- Fixture tests for zero-insight healthy runs, more-than-five rejection, duplicate/umbrella
  detection, cross-version overlap, low-confidence judge output, invalid judge JSON, existing-active
  bug, existing-resolved bug, direct-mail failure, trend rendering, and failure-email finalization.
- Container tests for hosted agents and immutable image digest deployment.
- Endpoint-only traffic tests that fail if any path attempts to write synthetic telemetry directly to
  Application Insights.
- Live integration tests against a disposable Foundry project before enabling the schedule.
- A shadow period with ADO/email side effects disabled is useful operational validation, but the
  configured quality verdict remains strict from the first run.

## 19. Risks and safeguards

- **Personal identity expiry or conditional access:** fail closed as `INCONCLUSIVE`; never fall back to
  stored secrets.
- **Copilot nondeterminism:** pin GPT-5.6 Sol, version/hash prompts and schemas, bound each evidence
  bundle, validate JSON, and use a blinded second pass for bug candidates.
- **Full-catalog cost/quota:** cap per-deployment concurrency and traffic volume; a budget/quota stop
  is `INCONCLUSIVE`, not a partial pass.
- **Trace ingestion delay:** poll exact trace IDs with a bounded deadline; never judge a partial trace
  set as complete.
- **Daily project propagation:** make the reviewed Bicep deployment idempotent, then poll the exact
  preprovisioned project and connections for readiness after a bounded 15-minute project-MI/ACR
  propagation gate. Poll a provisioning `CodeError` on the exact created agent version for a bounded
  15-minute grace without creating a duplicate; a persistent error is operational, not a quality
  verdict.
- **Synthetic health/finance data sensitivity:** use generated fictitious identities and no real
  medical or financial records.
- **Automation self-modification:** generated-path allowlist and branch protection prevent changes to
  baselines, catalog, policies, or prompts.
- **Duplicate ADO bugs:** stable fingerprint marker plus active/resolved ADO search is mandatory.
- **Email contradiction or injection:** one canonical report model, deterministic consistency checks,
  HTML encoding, direct-mail recipient allowlist, and a failure-email finalizer.
- **Mail capability unavailable:** retry, preserve the rendered failure report, and fail the GitHub
  automation visibly; do not claim an email was delivered.

## 20. Definition of done

The platform is complete when a scheduled GitHub Copilot run can:

1. Create the date-stamped project in the requested subscription/resource group/region.
2. Execute all five healthy agents and the complete fault catalog using production Agent Insights.
3. Produce a trustworthy strict scorecard and field-level judgments.
4. Preserve and reconcile quality history across days.
5. Create, update, or reopen only qualifying non-duplicate ADO bugs.
6. Commit each day's detailed plan/report and auto-merge only generated state/report changes.
7. Put a valid Agent Insights page link in every quality bug and in the email's all-agent table.
8. Deliver the polished four-section English report, including trend and failure-report behavior, by
   08:00 Pacific through the protected test-recipient variable during qualification; select the
   protected production-recipient variable only after explicit approval.
9. Retain projects for seven days and raw artifacts for 90 days without deleting unrelated resources.

## 21. References used

- Current Vienna Agent Insights source, contracts, benchmark, model matrix, and local-live E2E docs.
- Attached `Foundry-Insights-Assessment.docx`, especially its correct/partially useful/incorrect
  grading, capability evidence, product-gap analysis, and emphasis on complete trace and agent
  capability context.
- Microsoft Foundry agent evaluators:
  https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/agent-evaluators
- Microsoft Foundry agent evaluation workflow:
  https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/evaluate-agent
- Microsoft Foundry trace model and OpenTelemetry guidance:
  https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-agent-concept
- Microsoft Foundry hosted-agent deployment:
  https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent
