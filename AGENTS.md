# Agent Maintenance Guide

## Repository purpose

This public repository qualifies Microsoft Foundry Agent Insights with five fixed synthetic test
Agents and 36 reviewed, single-root issues.

## Non-negotiable safety rules

- Use only synthetic data and public-safe configuration.
- Never commit credentials, private Azure identifiers, internal endpoints, raw traces, complete
  prompt or response payloads, private work-item content, provider message IDs, or customer data.
- Invoke deployed Agent endpoints for test traffic.
- Keep Application Insights read-only. Direct trace injection is forbidden.
- Keep email requests, deployment registries, run manifests, assessment packages, assessments,
  promotion receipts, provider receipts, and work-item snapshots under `.aiq-runtime/`.

## Authorities

- `catalogs/AGENT_CATALOG.yaml` defines the five fixed Agents and profile contract.
- `catalogs/ISSUE_CATALOG.yaml` defines the 36 fixed issue contracts.
- Agent and Issue catalogs, Agent implementations, schemas, infrastructure, score policy, and
  promotion receipts require human review.
- Do not add compatibility readers or restore superseded identifiers and formats.
- Every issue folder is the complete deployable source authority for that version: Prompt issues own a
  full `definition.json`; Hosted issues own a full `source/` tree containing only that defect.
- Never ship runtime injection selectors, dormant branches for other issues, generic defect hooks, or
  build-time source patches inside an Agent version.

## Execution model

- `daily` and `staging` use separate Foundry accounts, Projects, telemetry, monitors, and private
  registries.
- Run the five test Agents concurrently.
- Within one Agent, run `v0` and issue versions sequentially.
- `gNN` is a telemetry generation: one App Insights and Log Analytics pair per profile.
- `rNN` is a qualification rerun identity.
- Rotate telemetry generation only for an explicit clean reset. Deploy and verify the new generation
  before deleting the old one.
- Telemetry generation is not an Agent deployment change and must not invalidate promotion receipts
  or Agent content digests.
- Monitor reset does not delete telemetry. Never reuse a dirty generation for a clean qualification.
- Never send ad-hoc debug traffic to daily or staging before a qualification.
- Quality-tagged Azure Boards work items are private email context only. Never write their query URL,
  titles, assignees, or links into committed reports.
- Bind each private work-item snapshot digest and closed-business date to its qualification run before
  Agent traffic; finalization must use the same snapshot.

## Assessment and scoring

- GPT-5.6 Sol assesses private packages against `src/agent_insights_quality/prompts/assessment.md`.
- Use independent `endpoint_evidence` and trace proof. Never use a card's own claim to prove the
  Agent defect described by that card.
- Equal nonzero request, response, and usable-response counts plus a verified natural trace contract
  prove that the reviewed source-and-traffic contract was exercised. Semantic assertions are optional.
- Assign `insight_engine` only when endpoint behavior and trace contract are proven.
- Use `none`, `agent`, `insight_engine`, `test_framework`, `infrastructure`, or `unresolved`
  ownership.
- `field_weighted_v1` score is 85% expected-issue field quality and 15% clean-card precision.
- Complete runs pass at 90 or above and fail below 90. Incomplete evidence is `INCOMPLETE` with no
  numeric score.

## Change workflow

1. Make the smallest coherent change.
2. Generate readable catalog views when a catalog changes.
3. Run repository validation, Ruff, and tests.
4. Compile Bicep when infrastructure changes.
5. Run full staging qualification for Agent, Issue, runtime, assessment, score, schema, or
   infrastructure changes.
6. Require human review before promotion to daily.

```powershell
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m ruff check .
python -m pytest
az bicep build --file infra\main.bicep --stdout
```

## Skills

- `.github/skills/agent-insights-quality-daily/SKILL.md`: weekday qualification and publication.
- `.github/skills/staging-qualification/SKILL.md`: full 36-issue qualification and promotion.
- `.github/skills/onboard-new-test-agent/SKILL.md`: add one reviewed fixed Test Agent.
- `.github/skills/onboard-new-issue/SKILL.md`: add one reviewed issue.
