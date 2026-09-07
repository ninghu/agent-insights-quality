# Agent Insights Quality

This public synthetic benchmark measures whether Foundry Agent Insights correctly identifies
known Agent defects without misleading or duplicate findings.

## Rewrite status

The replacement Python runner, Agent repairs and offline suites are integrated. Legacy
Copilot orchestration, assessment handoffs and promotion machinery have been removed.
Deployed staging and the real private TEST email are still required before enabling scheduled
qualification. Historical reports remain unchanged.

## Test inventory

| Agent | Implementation | Issues |
| --- | --- | ---: |
| Weather | Pure Prompt | 6 |
| Healthcare | Pure Prompt | 5 |
| Finance | Microsoft Agent Framework | 8 |
| Travel | LangGraph | 8 |
| Support | Responses host | 9 |

The reviewed catalogs are [AGENT_CATALOG.yaml](catalogs/AGENT_CATALOG.yaml) and
[ISSUE_CATALOG.yaml](catalogs/ISSUE_CATALOG.yaml). Their readable views are
[Agent Catalog](AGENT_CATALOG.md) and [Issue Catalog](ISSUE_CATALOG.md).
Every baseline/issue owns complete deployable source; no runtime issue selectors or source patches.
Issue-007 is a Support handoff serialization defect, not a Healthcare Prompt-policy exception.
The [maintenance contract](CONTRIBUTING.md#support-handoff-and-inventory-history) explains its
visible healthy requirement, baseline coverage and preservation of older inventory snapshots.

## Qualification design

- Local tests exercise actual Hosted business/framework code with controlled external boundaries
  and in-memory tracing. Prompt runtime behavior is evaluated in staging.
- Staging tests changed or missing targets. The initial full inventory is five baselines plus
  36 issues, with ten attempts each and no deployed paired-v0. Qualification requires eight
  adequately evidenced observations; a proven baseline or deterministic-contract violation
  still disqualifies the version. Scoped single-root hygiene separately checks for additional
  independent Agent defects; unresolved material candidates or inadequate evidence stay
  incomplete. Staging policy changes do not rewrite historical results.
- Daily runs all five Agents concurrently. Within each Agent, baseline and four rotated issue
  versions run sequentially, with six attributable trace-present attempts required out of ten.
- Sol assesses raw evidence directly. Correctness, Noise and Duplicate classification are separate
  from execution failure. Candidate gaps receive a bounded no-new-traffic evidence review.
- New scoring v2 uses `100 * C / (C + N_scored + 0.5 * D_scored + 0.25 * M)`,
  where `M = E_scored - C`. Historical results keep their recorded policy. Baseline penalties count;
  severity and suggested fixes are diagnostic. At most two unscorable version units permit a
  report with numeric coverage and exclusion reasons, without visible Full/Partial labels.
- Python owns orchestration and checkpoints. Copilot app automation launches it and performs only
  the final send from the generated recipient/HTML request. The user enables the weekday schedule.
- The [single automation bootstrap](.github/copilot/daily-bootstrap-prompt.md) takes only
  REPORT_MODE (`test` or `official`) and one TO_ADDRESS. Python allocates private TEST numbers
  and freezes routing/source. Eligible official mail uses the authorized frozen To; failures
  always use the private fallback. Legacy official mail remains fixed to TEAM_RECIPIENT.
- Reports are archived in existing private Storage, with a separate compact report per Agent
  and approved expiring read-only links. Generated reports do not create Git branches, PRs or
  merges; source and catalog changes still require normal review.

Both environments reuse their Sweden Central Accounts/Projects and Agent objects:
`aiq-staging-swedencentral` and `aiq-daily-swedencentral`.
Test Agents use GPT-5.4 mini, Insights uses GPT-5.6 Terra, and assessment uses GPT-5.6 Sol.
`infra/assessment-models.bicep` deploys only the two Sol assessment model deployments.
Both Bicep entry points default `assessmentCapacity` to 1000 per account, retaining
DataZoneStandard, model version `2026-07-09`, and NoAutoUpgrade. Agent and Insights
deployment capacities are independent and unchanged.

## Offline development

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m pytest
python -m ruff check src tests agents pyproject.toml
```

Default tests require no Azure/Hosted SDK, credentials, network, Docker or live model calls.
Explicit `tests/hosted` suites use each Agent's pinned requirements in isolated environments;
they are not collected by default.

See [Operations](docs/OPERATIONS.md) for staging, Daily and checkpoint recovery,
[Quality rules](docs/QUALITY_BAR.md) for evidence/scoring, and
[App automation](docs/AUTOMATION_SETUP.md) for the one-command launch and email handoff.
The [quality dashboard](docs/QUALITY_DASHBOARD.md) shows stored-score trends, changes in
Noise/Duplicate/missed counts, and coverage-aware Agent/issue drill-down.

## Skills

Skills are thin task entry points, not another orchestrator or a duplicate policy authority.

| Task | Skill |
| --- | --- |
| Maintain an Agent or issue; explicitly review inventory changes | [maintain-test-agents](.github/skills/maintain-test-agents/SKILL.md) |
| Launch authorized staging or resume its checkpoints | [staging-qualification](.github/skills/staging-qualification/SKILL.md) |
| Launch Daily and hand off the exact prepared email | [agent-insights-quality-daily](.github/skills/agent-insights-quality-daily/SKILL.md) |

Application Insights is read-only. Formal qualification invokes deployed endpoints.
Credentials, raw evidence, provider identifiers, checkpoints, logs and email requests stay under
the private `$HOME\.aiq-runtime\agent-insights-quality\` root, never Git or public ADX.
See [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md) for contributor boundaries.
