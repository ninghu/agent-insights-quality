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
  legacy promotion receipts, Test Agent Validation lifecycle/evidence/receipts, provider receipts,
  work-item snapshots, ADX publication receipts, and rendered dashboards under the durable user-level
  `~/.aiq-runtime/agent-insights-quality/` root shared by all worktrees.
- Canonical daily and staging deployment registries live in the private Azure
  `deployment-registries` blob container and synchronize into the local runtime root. Never commit
  them to Git.

## Authorities

- `catalogs/AGENT_CATALOG.yaml` defines the five fixed Agents and profile contract.
- `catalogs/ISSUE_CATALOG.yaml` defines the 36 fixed issue contracts.
- Agent and Issue catalogs, Agent implementations, validation modes/rules, schemas, infrastructure,
  score policy, trusted policy, and receipts require human review.
- Do not add compatibility readers or restore superseded identifiers and formats.
- Every issue folder is the complete deployable source authority for that version: Prompt issues own a
  full `definition.json`; Hosted issues own a full `source/` tree containing only that defect.
- Catalog Prompt Test Agents are pure Prompt: definitions never declare tools, traffic never contains
  tool fixtures, and any emitted function call fails the qualification contract.
- Never ship runtime injection selectors, dormant branches for other issues, generic defect hooks, or
  build-time source patches inside an Agent version.

## Execution model

- Official Daily and Test Agent Validation use separate Foundry accounts and telemetry. Daily keeps
  its Project and monitors; validation creates one temporary Project and no monitor.
- Run the five test Agents concurrently.
- Stagger Agent starts by the reviewed short delay to avoid a simultaneous endpoint burst.
- Within one Agent, run `v0` and issue versions sequentially.
- Daily rotates exactly four issues per Agent: 20 issues plus five baselines, for 25 packages.
- Daily is single-region. Read `location` from the concrete Daily Foundry Project with ARM, resolve
  its display through Azure location metadata, and use registry/config values only as cross-checks.
  Never hardcode a renderer fallback or add multi-region behavior.
- Validation deploys 41 independent endpoints: five baselines plus all 36 issues.
- Baselines require `5/5` healthy attempts. Deterministic issues require `5/5` with paired `v0`
  `0/5`; model-mediated issues require `5/7` with paired `v0` `0/7`.
- Validation mode is reviewed catalog data bound into `execution_digest`; never infer or reclassify
  it from runtime results, resample a miss, or lower `n`/`k`.
- Validation is report-free: never create monitors, run Agent Insights, assess/score cards, publish
  ADX, send email, or run Daily from a validation cycle.
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
- Never send ad-hoc debug traffic to Daily or the validation account.
- Quality-tagged Azure Boards work items are private email context only. Never write their query URL,
  titles, assignees, or links into committed reports.
- Bind each private work-item snapshot digest and closed-business date to its qualification run before
  Agent traffic; finalization must use the same snapshot.
- A temporary individual test recipient may be configured only in the durable private runtime root;
  the committed fallback recipient remains the reviewed team mailbox.
- An email-only test run must explicitly use `--test-run` with a nonzero rerun identity. It sends only
  to the private test recipient and never writes ADX, repository report or trend paths, or a pull
  request. Official runs ignore the private recipient override.

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
- Official Daily builds a public-safe normalized improvement input after assessments. GPT-5.6 Sol
  returns only the strict improvement-analysis schema; deterministic code reconciles living pattern
  state. Only `insight_engine` ownership is eligible, and the result is advisory and score-neutral.
- Publish `reports/insight-engine-improvement.{json,md}` and the immutable dated snapshot with the
  Daily report. Email-only tests write a private preview only and never link or mutate living memory.
- Complete runs pass at 90 or above and fail below 90. Incomplete evidence is `INCOMPLETE` with no
  numeric score.

## Change workflow

1. Make the smallest coherent change.
2. Generate readable catalog views when a catalog changes.
3. Run repository validation, Ruff, and tests.
4. Compile Bicep when infrastructure changes.
5. Run one Test Agent Validation cycle. The first pass always covers all 41; fixes may reuse only
   unchanged current-digest evidence within that cycle. Shared validation-contract changes invalidate
   all 41 and require a new cycle.
6. Freeze scope, complete exactly one comprehensive review, run targeted finding verification and CI
   on the exact final head, then clean every cycle resource exactly.
7. Require a create-once protected merge receipt. Shadow receipts never authorize merge.
8. Keep the legacy staging path unchanged only for migration retention; `r03` is final and staging is
   never a fallback.

```powershell
python -m agent_insights_quality generate-docs
python -m agent_insights_quality generate-test-agent-validation-rules --check
python -m agent_insights_quality validate
python -m ruff check .
python -m pytest
az bicep build --file infra\main.bicep --stdout
```

## Skills

- `.github/skills/agent-insights-quality-daily/SKILL.md`: weekday qualification and publication.
- `.github/skills/test-agent-validation/SKILL.md`: protected 41-authority candidate gate.
- `.github/skills/staging-qualification/SKILL.md`: retained legacy `r03` history; do not execute.
- `.github/skills/onboard-test-agent/SKILL.md`: add one reviewed fixed Test Agent.
- `.github/skills/onboard-new-issue/SKILL.md`: add one reviewed issue.
