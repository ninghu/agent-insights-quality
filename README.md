# Agent Insights Quality

This repository qualifies Microsoft Foundry Agent Insights against five fixed synthetic test Agents.
It uses independent daily and staging Foundry accounts, each with one Project:

- `agent-insights-quality` for weekday qualification;
- `agent-insights-quality-staging` for full qualification before promotion.

Both profiles have independent Application Insights resources, monitors, deployment registries, and
private artifacts. Application Insights is read-only. Test traffic always invokes exact deployed
Agent versions; direct trace injection is forbidden.

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

All five test Agents use GPT-5.6 Terra. Agent Insights generation uses a separate GPT-5.6 Terra
deployment. GPT-5.6 Sol performs one structured quality assessment.

## Daily qualification

Monday through Friday, each Agent runs:

1. its exact `v0` version, which must produce zero Insights;
2. five deterministically rotated issue versions;
3. one on-demand Agent Insights run after each exact version's telemetry arrives.

The first run after monitor reset uses the minimum three-hour lookback. The service checkpoint
advances later effective windows. Exact Agent version and operation IDs remain mandatory evidence.

The daily quality score is 85% field quality across expected issues and 15% clean-card precision.
A complete run is `PASS` at `90/100` or above and `FAIL` below `90/100`; incomplete or ambiguous
evidence is `INCOMPLETE`.

## Commands

```powershell
python -m pip install -e ".[dev]"
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m pytest
```

Live commands require protected runtime configuration:

```powershell
python -m agent_insights_quality deploy-infrastructure
python -m agent_insights_quality provision --profile staging
python -m agent_insights_quality fetch-quality-work-items `
  --query-url <private-query-url> `
  --report-date <Pacific YYYY-MM-DD> `
  --output .aiq-runtime\work-items\active-quality.json
python -m agent_insights_quality run-full --report-date <Pacific YYYY-MM-DD> `
  --work-items .aiq-runtime\work-items\active-quality.json
python -m agent_insights_quality provision --profile daily
python -m agent_insights_quality run-daily --report-date <Pacific YYYY-MM-DD> `
  --work-items .aiq-runtime\work-items\active-quality.json
```

Infrastructure deployment resolves the latest GPT-5.6 Terra version available in West US 2 from the
Azure ARM model catalog.

See [Framework Overview](docs/FRAMEWORK_OVERVIEW.md), [Operations](docs/OPERATIONS.md),
[Automation Setup](docs/AUTOMATION_SETUP.md), and [Quality Bar](docs/QUALITY_BAR.md).
