# Agent Insights Quality

This repository qualifies Microsoft Foundry Agent Insights against five fixed synthetic test Agents.
Official Daily and local Test Agent Validation use separate durable Sweden Central environments:

- `aiq-daily-swedencentral` Account and Project for weekday qualification;
- `aiq-staging-swedencentral` Account and Project for the human-reviewed staging gate.

Validation reconciles stable baseline/issue Agent names to exact server-assigned provider versions.
It first runs the official `weather-agent/v0` Prompt and `finance-agent/v0` Hosted baselines; only
when both pass does it deploy the remaining 39 and run the five independent Agent lanes. A
pre-traffic transient subset may resume only within the unchanged local cycle.

Application Insights is read-only. Validation uses the staging `g30` Sweden telemetry pair, creates no
monitor, runs no Agent Insights assessment or report, retains the durable Project and stable Agent
topology, and deletes only run-scoped resources.
Test traffic always invokes exact deployed Agent endpoints; direct trace injection is forbidden.
Lifecycle, content-addressed history, evidence, and CLEAN proof stay only under the shared
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

Daily is currently single-region. Before traffic, automation reads the concrete Daily Foundry
Project's ARM `location`, resolves its public display through Azure location metadata, and cross-checks
the private registry. Reports and email show `SwedenCentral`; missing or mismatched region proof
fails closed, and the renderer does not supply a fallback.

## Local Test Agent Validation

Every clean commit validates five baselines and all 36 issues through 41 independent Agent endpoints.
Each reviewed scenario reruns its setup and probe conversation with a fresh identity. Baselines require
`5/5` healthy attempts. Deterministic defects require `5/5` observations and paired `v0` at `0/5`;
model-mediated defects require at least `5/7` and paired `v0` at `0/7`. The reviewed mode is catalog
data bound into the automatic `execution_digest`; runtime results cannot reclassify, resample, or lower
the threshold.

An environment-namespaced OS file lock excludes concurrent worktrees. The local atomic journal, required
content-addressed history, and 72-hour execution TTL support same-commit cleanup recovery. Any commit
change cleans run-scoped cycle state and requires a fresh full run. After 41/41 evidence and exact CLEAN,
the user may run a separate approval command that rechecks the current PR head and creates one minimal
immutable approved Blob record. GitHub provides ordinary mechanical CI only; merge remains manual.

The runner sends no Daily smoke traffic and imposes no unconditional pre-traffic delay. It correlates
natural telemetry by exact run, Agent, provider version, operation, and invocation time window, then
waits within the bounded post-invoke hydration and stability deadline. Missing attributable traces
fail closed.

The daily quality score is 85% field quality across expected issues and 15% clean-card precision.
Internally, a complete run stores `PASS` at `90/100` or above and `FAIL` below `90/100`.
User-facing reports present the numeric score and finding details without those verdict labels;
incomplete or ambiguous evidence remains explicitly `INCOMPLETE`.

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
python -m agent_insights_quality run-test-agent-validation
# Run only after explicit human approval of the exact CLEAN result:
python -m agent_insights_quality approve-test-agent-validation
python -m agent_insights_quality fetch-quality-work-items `
  --query-url <private-query-url> `
  --report-date <Pacific YYYY-MM-DD> `
  --output $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
# Daily fetches the current clean commit's immutable approved record from Blob.
python -m agent_insights_quality provision --profile daily
python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD> `
  --work-items $HOME\.aiq-runtime\agent-insights-quality\work-items\active-quality.json
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
