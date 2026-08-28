# Agent Maintenance Guide

## Repository purpose

This public repository qualifies Microsoft Foundry Agent Insights with five fixed synthetic test
Agents and 36 reviewed, single-root issues.

## Non-negotiable safety rules

- Use only synthetic data and public-safe configuration.
- Never commit credentials, private Azure identifiers, internal endpoints, raw traces, complete
  prompt or response payloads, private work-item content, provider message IDs, or customer data.
- Invoke deployed Agent endpoints for test traffic.
- Keep Application Insights read-only. Direct trace injection is forbidden. ADX is the only
  authorized analytics write and receives only public-safe catalog context plus fields already
  present in committed sanitized daily reports. Never publish private assessment packages or
  work-item context to ADX.
- Keep email requests, deployment registries, run manifests, assessment packages, assessments,
  promotion receipts, provider receipts, work-item snapshots, ADX publication receipts, rendered
  dashboards under the durable user-level
  `~/.aiq-runtime/agent-insights-quality/` root shared by all worktrees.
- Canonical daily and staging deployment registries live in the private Azure
  `deployment-registries` blob container and synchronize into the local runtime root. Never commit
  them to Git.

## Authorities

- `catalogs/AGENT_CATALOG.yaml` defines the five fixed Agents and profile contract.
- `catalogs/ISSUE_CATALOG.yaml` defines the 36 fixed issue contracts.
- Agent and Issue catalogs, Agent implementations, schemas, infrastructure, score policy, and
  promotion receipts require human review.
- Do not add compatibility readers or restore superseded identifiers and formats.
- Every issue folder is the complete deployable source authority for that version: Prompt issues own a
  full `definition.json`; Hosted issues own a full `source/` tree containing only that defect.
- Catalog Prompt Test Agents are pure Prompt: definitions never declare tools, traffic never contains
  tool fixtures, and any emitted function call fails the qualification contract.
- Never ship runtime injection selectors, dormant branches for other issues, generic defect hooks, or
  build-time source patches inside an Agent version.

## Execution model

- `daily` and `staging` use separate Foundry accounts, Projects, telemetry, monitors, and private
  registries.
- Run the five test Agents concurrently.
- Stagger Agent starts by the reviewed short delay to avoid a simultaneous endpoint burst.
- Within one Agent, run `v0` and issue versions sequentially.
- Every potentially long-running operation emits public-safe start, elapsed heartbeat, and
  completion/failure progress. Progress-output failures must never abort the operation.
- `g29` is the fixed telemetry resource set: one App Insights and Log Analytics pair per profile.
- `rNN` is a qualification rerun identity.
- Routine runs and reruns reuse `g29`; they must not create or rotate telemetry resources.
- The telemetry resource set is not an Agent deployment change and must not invalidate promotion
  receipts or Agent content digests.
- Monitor reset does not delete telemetry. Wait for the reviewed `0.1`-hour clean interval before a
  new traffic attempt.
- Recover at most three transiently incomplete versions per Agent before declaring the run incomplete.
- Never send ad-hoc debug traffic to daily or staging before a qualification.
- Quality-tagged Azure Boards work items are private email context only. Never write their query URL,
  titles, assignees, or links into committed reports.
- Bind each private work-item snapshot digest and closed-business date to its qualification run before
  Agent traffic; finalization must use the same snapshot.
- A temporary individual test recipient may be configured only in the durable private runtime root;
  the committed fallback recipient remains the reviewed team mailbox.

## Assessment and scoring

- GPT-5.6 Sol assesses private packages against `src/agent_insights_quality/prompts/assessment.md`.
- Use independent `endpoint_evidence` and trace proof. Never use a card's own claim to prove the
  Agent defect described by that card.
- Equal nonzero request, response, and usable-response counts plus a verified natural trace contract
  prove only endpoint execution. Baselines also require every reviewed semantic assertion and one
  independently proven terminal success with output per request.
- Assess card-linked proof against full-request proof. An intermediate-operation contradiction is
  `test_framework` or `unresolved`, never an unsupported Agent or Insight Engine conclusion.
- A handled child error may coexist with a successful terminal response; any unhandled baseline error
  keeps the run incomplete.
- Assign `insight_engine` only when endpoint behavior and trace contract are proven.
- Baseline source and configuration must be reviewed healthy, but runtime behavior must still be
  proven. A baseline card supported by independent trace proof is an `agent` finding, not Noise;
  external identity, quota, service, or ingestion failures use `infrastructure`.
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
5. Use impact-based staging:
   - Agent source, definition, traffic, or assigned-issue changes qualify only each affected Agent's
     `v0` and all assigned issues.
   - Unchanged Agents reuse their latest reviewed evidence only when their content digests, mappings,
     and every shared runtime contract are unchanged.
   - Shared runtime, telemetry, assessment, scoring, schema, infrastructure, or cross-Agent topology
     changes require full-catalog qualification, or retained evidence re-evaluation when no new Agent
     traffic is needed.
6. Compose promotion from new affected-Agent evidence plus the latest valid receipts for unchanged
   Agents. Every exact digest and mapping must match; `INCOMPLETE` evidence is never reusable.
7. Require human review before promotion to daily.
8. After exact-digest daily provisioning, use read-only readiness and registry reconciliation. Do not
   send daily smoke traffic that dirties the clean window.

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
- `.github/skills/onboard-test-agent/SKILL.md`: add one reviewed fixed Test Agent.
- `.github/skills/onboard-new-issue/SKILL.md`: add one reviewed issue.
