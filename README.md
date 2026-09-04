# Agent Insights Quality

This repository qualifies Microsoft Foundry Agent Insights against five fixed synthetic test Agents.
Official Daily and local Test Agent Validation use separate durable Sweden Central environments:

- `aiq-daily-swedencentral` Account and Project for weekday qualification;
- `aiq-staging-swedencentral` Account and Project for advisory human-reviewed validation.

Validation reconciles each unique baseline/issue Agent identity to an exact server-assigned
provider version. A fresh advisory run validates all 41 authorities. Automatic recovery reuses
unchanged deployment, traffic, and definitive results only from its immediately preceding
generation.

Application Insights is read-only. Validation uses the staging `g30` Sweden telemetry pair, creates no
monitor, runs no Agent Insights assessment or report, and retains the durable Project, Agents,
versions, sessions, responses, images, telemetry, registries, and evidence.
Test traffic always invokes exact deployed Agent endpoints; direct trace injection is forbidden.
Lifecycle, generation evidence, and retained deployment receipts stay only under
the shared `~/.aiq-runtime/agent-insights-quality/test-agent-validation/` root.

Sanitized daily results are published to a shared Azure Data Explorer database in the same
quality-test resource group. A native ADX dashboard combines run, Agent, issue, card, and field
trends with public catalog expectations and the explanations already present in committed reports.
It never stores prompts, responses, traces, evidence references, work items, private runtime links,
or private Azure identifiers.

## Reviewed contracts

Only two catalogs define the test inventory:

- [`catalogs/AGENT_CATALOG.yaml`](catalogs/AGENT_CATALOG.yaml)
- [`catalogs/ISSUE_CATALOG.yaml`](catalogs/ISSUE_CATALOG.yaml)

The generated [Agent Catalog](AGENT_CATALOG.md) and [Issue Catalog](ISSUE_CATALOG.md) are readable
views. The Issue Catalog contains 36 single-root issues. Each issue belongs permanently to one Agent
version and expects exactly one Insight.

## Agents

| Agent | Foundry type | Implementation | Issue count |
| --- | --- | --- | ---: |
| `weather-agent` | Prompt | Foundry Prompt | 6 |
| `healthcare-agent` | Prompt | Foundry Prompt | 6 |
| `finance-agent` | Hosted code | Microsoft Agent Framework | 8 |
| `travel-agent` | Hosted code | LangGraph | 8 |
| `support-ticket-agent` | Hosted container | Custom Responses | 8 |

All five test Agents use GPT-5.4 mini. Agent Insights generation uses a separate GPT-5.6 Terra
deployment. GPT-5.6 Sol performs one structured quality assessment.

## Daily qualification

Monday through Friday, each Agent runs:

1. its exact `v0` version, which must produce zero Insights;
2. four deterministically rotated issue versions;
3. one on-demand Agent Insights run after each exact version's telemetry arrives.

Daily therefore evaluates 20 issues plus five baselines (25 assessment packages). Sweden staging
qualification validates all 36 issues plus five baselines as advisory evidence for the human
operator. Daily admission does not read staging state or validation digests; the
human decision is represented only by manually invoking `daily-prepare`.

Daily concurrency is orchestrated only through visible Copilot sub sessions. Five traffic lanes run
`v0` plus four issues sequentially, up to eight evaluators verify the resulting 25 immutable receipts,
then five Agent Insights lanes process each Agent's verified versions. Global barriers separate the
phases; no hidden pool or per-issue paired-`v0` traffic exists.

Daily is currently single-region. Before traffic, automation reads the concrete Daily Foundry
Project's ARM `location`, resolves its public display through Azure location metadata, and cross-checks
the private registry. Reports and email show `SwedenCentral`; missing or mismatched region proof
fails closed, and the renderer does not supply a fallback.

## Local Test Agent Validation

Validation covers five baselines and all 36 issues through 41 independent Agent endpoints. Staging
is advisory, so a fresh run does not maintain or consult a global PASS history. A recovery generation
contains one integrity-bound `recovery_source` reference and flattened immutable result/receipt
references from that source. This preserves zero-traffic first recollection without scanning older
runs or making staging a Daily gate.
Each reviewed scenario reruns its setup and probe conversation with a fresh identity. Baselines,
issues, and paired controls each pass with at least six complete role-specific passes out of ten.
The other four attempts remain transparent misses of any kind and never veto six strict passes.
Every passing attempt still requires complete exact endpoint, identity, required semantic/trace,
and internally consistent proof. The reviewed mode is catalog
data bound into the automatic `execution_digest`; runtime results cannot reclassify, resample, or lower
the threshold.

One visible Copilot coordinator owns the run. It publishes immutable deployment, invocation, and
verification assignments, releases the coordinator lock, and creates visible Copilot sub-sessions for
parallel work; validation never hides parallelism in subprocesses, `ThreadPoolExecutor`, or another
in-process pool. Each non-empty phase independently has one to eight deterministic, cost-balanced
logical shards based on its selected authorities. Every active shard maps 1:1 to one visible
sub-session, so eight is both the per-phase shard ceiling and the maximum active concurrency.

Immediately after definitive authority completion, the sub-session atomically publishes a
generation-fenced invocation receipt. Only an automatic successor recovery generation may reuse the
source generation's exact receipt. Response/session references, invoke/evidence windows, and complete
provenance remain fully validated. Unknown, ambiguous, duplicate, partial, or indeterminate
retried-POST outcomes are never reusable.
After all selected evaluations finish with any `INCOMPLETE` result, the explicit recovery command
atomically advances to and reconciles a same-head successor generation. Its first exact-receipt
recollection has no deployment or invocation assignments. If that immutable recollection remains
`INCOMPLETE`, the reviewed policy selects fresh issue plus paired-`v0` traffic in the following
generation. All other selected authorities
perform verification only. The reused receipt proves the traffic-generation and execution contract;
every new verification package separately binds that receipt's immutable digest and the current
verifier commit and digest.

Verification uses up to eight visible GPT-5.6 Sol Copilot evaluator sessions. Each uses a hidden
worktree-bound lease to claim exactly one distinct authority at a time, prepares a content-addressed
private package, imports one strict public-safe behavioral evaluation, and atomically persists the
immutable result before claiming the next authority. An evaluator that must stop before import
explicitly releases only its own generation-fenced claim, invalidating the stale package and making
the authority immediately claimable; lease expiry remains the crash fallback. Shared-lock contention
before assignment returns a structured retryable result without claiming work. Status exposes only
aggregate slot counts. A baseline uses one batched telemetry
stability snapshot for all ten attempts. An issue uses exactly two target batches: one snapshot for
all issue attempts and one for all paired-`v0` attempts. A fresh partial whole-target snapshot cannot
stabilize until every required trace surface is present; genuinely missing traces remain `INCOMPLETE`
at the bounded deadline. It never
stabilizes attempts independently, deploys an Agent, invokes an endpoint, or sends traffic.
When an exact reused receipt's evidence window is older than the configured hydration horizon plus
stability interval, the verifier performs one mature whole-target snapshot with no polling delay,
binds its as-of time, maturity boundary, timing policy, and receipt digest into the private evidence,
and records any remaining required trace gap as an explicit unknown for the shared reviewed
acceptance policy. Deterministic code enforces identity,
response/session coverage, endpoint response presence, terminal-output integrity, trace correlation,
privacy, and package binding; GPT-5.6 Sol judges every semantic, tool, trace, activation, health, and
per-attempt behavioral assertion.

Authority results keep `PASS`, `FAIL`, and `INCOMPLETE` distinct. Baselines, issues, and paired `v0`
controls require six complete healthy, defect-observed, or zero-defect control passes respectively.
The remaining four attempts never veto that aggregate PASS and retain their exact miss categories.
Complete evidence that misses the reviewed threshold is `FAIL`;
missing, ambiguous, or unstable evidence is `INCOMPLETE`, never `FAIL`. A later authority or
sub-session failure does not discard already persisted results. `run-test-agent-validation` reports
all five Agents and 41 authorities as `PASS`, `FAIL`, `INCOMPLETE`, or `missing`, with only public-safe
source/provider/traffic change reasons. It remains status-only.

The local atomic journal, content-addressed history, and a 72-hour execution TTL support resumable
work. Stale sub-sessions fail closed. Starting a new validation atomically supersedes incomplete local
state without cleanup or deletion of provider objects, invocation receipts, or evidence. Final
composition requires exact evidence for all 41 authorities. Staging status is advisory; the human
decision to run Daily is represented only by manually invoking Daily. GitHub provides ordinary
mechanical CI only; merge remains manual.

The runner sends no Daily smoke traffic and imposes no unconditional pre-traffic delay. It correlates
natural telemetry by exact run, Agent, provider version, operation, and invocation time window, then
waits within the bounded post-invoke hydration and stability deadline. Missing attributable traces
fail closed.

The daily quality score is the percentage of expected issues with a score-correct Insight, with
Noise and Duplicate cards added to the denominator. Title, description, category, and linked traces
determine correctness; severity and proposed fix are diagnostic only. Reports contain one numeric
score and no PASS/FAIL label or threshold. Incomplete evidence fails the qualification internally
and produces no report.

Official Daily also publishes a stable
[Insight Engine Improvement Memory](reports/insight-engine-improvement.md) plus one immutable
per-run snapshot. Only `insight_engine`-owned, public-safe findings can support a cross-Agent pattern;
the advisory memory never changes score, ownership, promotion, or Test Agent Validation.

## Commands

```powershell
python -m pip install -e ".[dev]"
python -m pip install -e ".[azure]"
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m pytest
```

Live commands use the authenticated local Azure CLI user and private runtime configuration:

```powershell
python -m agent_insights_quality deploy-infrastructure
python -m agent_insights_quality deploy-analytics
# The visible Copilot coordinator follows the test-agent-validation skill:
python -m agent_insights_quality prepare-test-agent-validation
# After a completed generation reports INCOMPLETE, atomically advance and reconcile:
python -m agent_insights_quality recover-test-agent-validation
# Status and next-action guide only; this command never creates sub-sessions:
python -m agent_insights_quality run-test-agent-validation
python -m agent_insights_quality fetch-quality-work-items `
  --query-url <private-query-url> `
  --report-date <Pacific YYYY-MM-DD> `
  --output $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
# The human decision to run Daily is the manual invocation; staging is not an admission input.
python -m agent_insights_quality daily-prepare --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
python -m agent_insights_quality daily-provision
python -m agent_insights_quality daily-guide
# One visible full-pipeline session per Agent; run these five concurrently.
python -m agent_insights_quality daily-run-agent --agent weather-agent
python -m agent_insights_quality daily-run-agent --agent healthcare-agent
python -m agent_insights_quality daily-run-agent --agent finance-agent
python -m agent_insights_quality daily-run-agent --agent travel-agent
python -m agent_insights_quality daily-run-agent --agent support-ticket-agent
python -m agent_insights_quality daily-compose
python -m agent_insights_quality daily-status
python -m agent_insights_quality render-adx-dashboard
```

Full infrastructure deployment creates only the Sweden Central profile resources in the existing
resource group. It pins `gpt-5.4-mini` `2026-03-17` at DataZoneStandard capacity 4500 and
`gpt-5.6-terra` `2026-07-09` at DataZoneStandard capacity 100 per account with `NoAutoUpgrade`;
there is no model or regional fallback. Shared ACR, ADX, and all West US 2 resources, including the
legacy unversioned storage account, are referenced or left untouched. The dedicated
`aiqsweart${uniqueSuffix}` Sweden g30 StorageV2 account owns private quality artifacts, deployment
registries. Use the scoped `deploy-analytics` command
to create or update only the two-node production ADX trend database in the existing
`agent-insights-quality-rg` without changing Foundry or telemetry. Daily finalization publishes
sanitized results and explanations there and includes the reviewed
`https://aka.ms/agent-insights/quality` short link in the HTML email.

See [Framework Overview](docs/FRAMEWORK_OVERVIEW.md), [Operations](docs/OPERATIONS.md),
[Automation Setup](docs/AUTOMATION_SETUP.md), [Insight Result Labels](docs/INSIGHT_RESULTS.md), and
[Quality Bar](docs/QUALITY_BAR.md).
