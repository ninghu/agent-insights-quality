# Agent Insights Quality

This repository qualifies Microsoft Foundry Agent Insights against five fixed synthetic test Agents.
Official Daily and local Test Agent Validation use separate durable Sweden Central environments:

- `aiq-daily-swedencentral` Account and Project for weekday qualification;
- `aiq-staging-swedencentral` Account and Project for the human-reviewed staging gate.

Validation reconciles each unique baseline/issue Agent identity to an exact server-assigned
provider version. It deploys only content-changed versions, reuses exact versions, and validates
only changed, missing, or `INCOMPLETE` authorities.

Application Insights is read-only. Validation uses the staging `g30` Sweden telemetry pair, creates no
monitor, runs no Agent Insights assessment or report, and retains the durable Project, Agents,
versions, sessions, responses, images, telemetry, registries, and evidence.
Test traffic always invokes exact deployed Agent endpoints; direct trace injection is forbidden.
Lifecycle, content-addressed history, evidence, and retained deployment receipts stay only under the shared
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/` root. Blob stores only the final
minimal approved record created after explicit human approval.

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

Daily therefore evaluates 20 issues plus five baselines (25 assessment packages). Official Sweden
staging qualification validates all 36 issues plus five baselines before exact approved digests may
be promoted to Daily.

Daily concurrency is orchestrated only through visible Copilot sub sessions. The central coordinator
prepares and provisions one private lifecycle, then assigns five whole-Agent lanes: Weather,
Healthcare, Finance, Travel, and Support. Each lane holds its own lock and runs `v0` followed by four
selected issues sequentially. Immutable lane receipts make interrupted work resumable and fence stale
workers. There is no Daily thread-pool, subprocess, or nested endpoint-request fan-out.

Daily is currently single-region. Before traffic, automation reads the concrete Daily Foundry
Project's ARM `location`, resolves its public display through Azure location metadata, and cross-checks
the private registry. Reports and email show `SwedenCentral`; missing or mismatched region proof
fails closed, and the renderer does not supply a fallback.

## Local Test Agent Validation

The first run under a changed shared contract validates five baselines and all 36 issues through 41
independent Agent endpoints. Later commits reuse exact completed authority results and run only
authorities whose content or binding changed, whose latest result is `INCOMPLETE`, or whose exact
result is missing. A definitive `FAIL` remains complete and is not retried unless its binding changes.
Each reviewed scenario reruns its setup and probe conversation with a fresh identity. Baselines require
`5/5` healthy attempts. Deterministic defects require `5/5` observations and paired `v0` at `0/5`;
model-mediated defects require at least `5/7` and paired `v0` at `0/7`. The reviewed mode is catalog
data bound into the automatic `execution_digest`; runtime results cannot reclassify, resample, or lower
the threshold.

One visible Copilot coordinator owns the run. It publishes immutable deployment, invocation, and
verification assignments, releases the coordinator lock, and creates visible Copilot sub-sessions for
parallel work; validation never hides parallelism in subprocesses, `ThreadPoolExecutor`, or another
in-process pool. Each non-empty phase independently has one to eight deterministic, cost-balanced
logical shards based on its selected authorities. Every active shard maps 1:1 to one visible
sub-session, so eight is both the per-phase shard ceiling and the maximum active concurrency.

Immediately after definitive authority completion, the sub-session atomically publishes a
generation-fenced invocation receipt. A later hidden, system-generated validation generation or
verifier-only commit may reuse it only when the exact Agent source, provider content, execution
contract, provider-assigned version, runtime, environment, Project, telemetry resource set,
response/session references, invoke/evidence windows, and complete issue/paired-`v0` provenance still
match. Unknown, ambiguous, duplicate, partial, or indeterminate retried-POST outcomes are never
reusable.

Therefore the next generation selects only changed, `INCOMPLETE`, or missing authorities.
Within that set, it invokes only authorities without a current exact-bound completed invocation
receipt; all others perform verification only and send no new endpoint traffic. The reused receipt
proves the traffic-generation and execution contract; every new verification package separately binds
that receipt's immutable digest and the current verifier commit and digest.

Verification uses up to eight visible GPT-5.6 Sol Copilot evaluator sessions. Each uses a hidden
worktree-bound lease to claim exactly one distinct authority at a time, prepares a content-addressed
private package, imports one strict public-safe behavioral evaluation, and atomically persists the
immutable result before claiming the next authority. Status exposes only aggregate slot counts, and an
abandoned claim becomes reclaimable after its bounded lease. A baseline uses one batched telemetry
stability snapshot for all five attempts. An issue uses exactly two target batches: one snapshot for
all issue attempts and one for all paired-`v0` attempts. It never stabilizes attempts independently,
deploys an Agent, invokes an endpoint, or sends traffic. Deterministic code enforces identity,
response/session coverage, endpoint response presence, terminal-output integrity, trace correlation,
privacy, and package binding; GPT-5.6 Sol judges every semantic, tool, trace, activation, health, and
per-attempt behavioral assertion.

Authority results keep `PASS`, `FAIL`, and `INCOMPLETE` distinct. Baselines pass only at `5/5`;
deterministic issues pass only at `5/5` with paired `v0` at `0/5`; model-mediated issues pass at
`>=5/7` with paired `v0` at `0/7`. Complete evidence that misses the reviewed threshold is `FAIL`;
missing, ambiguous, or unstable evidence is `INCOMPLETE`, never `FAIL`. A later authority or
sub-session failure does not discard already persisted results. Retries schedule only missing,
`INCOMPLETE`, or binding-changed authorities.

The local atomic journal, content-addressed history, and a 72-hour execution TTL support resumable
work. Stale sub-sessions fail closed. Starting a new validation atomically supersedes incomplete local
state without cleanup or deletion of provider objects, invocation receipts, or evidence. Final
composition requires exact evidence for all 41 authorities. After exact current-head 41/41 PASS
evidence, the user may run a separate approval command that rechecks the current PR head and creates
one minimal immutable approved Blob record. GitHub provides ordinary mechanical CI only; merge
remains manual.

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
# Status and next-action guide only; this command never creates sub-sessions:
python -m agent_insights_quality run-test-agent-validation
# Run only after explicit human approval of the exact READY result:
python -m agent_insights_quality approve-test-agent-validation
python -m agent_insights_quality fetch-quality-work-items `
  --query-url <private-query-url> `
  --report-date <Pacific YYYY-MM-DD> `
  --output $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
# Daily binds the current clean commit's immutable approved record from Blob.
python -m agent_insights_quality daily-prepare --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
python -m agent_insights_quality daily-provision
python -m agent_insights_quality daily-guide
# Run each returned command in its own visible Copilot sub session.
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
registries, and the environment-namespaced approved-record container with its locked 90-day WORM
policy. Use the scoped `deploy-analytics` command
to create or update only the two-node production ADX trend database in the existing
`agent-insights-quality-rg` without changing Foundry or telemetry. Daily finalization publishes
sanitized results and explanations there and includes the reviewed
`https://aka.ms/agent-insights/quality` short link in the HTML email.

See [Framework Overview](docs/FRAMEWORK_OVERVIEW.md), [Operations](docs/OPERATIONS.md),
[Automation Setup](docs/AUTOMATION_SETUP.md), [Insight Result Labels](docs/INSIGHT_RESULTS.md), and
[Quality Bar](docs/QUALITY_BAR.md).
