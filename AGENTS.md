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
  legacy promotion receipts, Test Agent Validation lifecycle/history/desired-state/receipts/evidence, approved
  validation records, provider receipts,
  work-item snapshots, ADX publication receipts, and rendered dashboards under the durable user-level
  `~/.aiq-runtime/agent-insights-quality/` root shared by all worktrees.
- Canonical daily and staging deployment registries live in the dedicated Sweden Central `g30`
  storage account's private `deployment-registries` blob container and synchronize into the local
  runtime root. Never commit them to Git or fall back to the retained legacy storage account.

## Authorities

- `catalogs/AGENT_CATALOG.yaml` defines the five fixed Agents and profile contract.
- `catalogs/ISSUE_CATALOG.yaml` defines the 36 fixed issue contracts.
- Agent and Issue catalogs, Agent implementations, validation modes/rules, schemas, infrastructure,
  score policy, and approved validation records require human review.
- Do not add compatibility readers or restore superseded identifiers and formats.
- Every issue folder is the complete deployable source authority for that version: Prompt issues own a
  full `definition.json`; Hosted issues own a full `source/` tree containing only that defect.
- Catalog Prompt Test Agents are pure Prompt: definitions never declare tools, traffic never contains
  tool fixtures, and any emitted function call fails the qualification contract.
- Never ship runtime injection selectors, dormant branches for other issues, generic defect hooks, or
  build-time source patches inside an Agent version.

## Execution model

- Official Daily and Test Agent Validation use the durable
  `aiq-daily-swedencentral` and `aiq-staging-swedencentral` Foundry accounts and identically named
  Projects with separate Sweden Central telemetry. Validation creates no monitor.
- For Daily, run the five test Agents concurrently, stagger starts by the reviewed short delay, and
  run `v0` and issue versions sequentially within one Agent.
- Daily rotates exactly four issues per Agent: 20 issues plus five baselines, for 25 packages.
- Daily is single-region. Read `location` from the concrete Daily Foundry Project with ARM, resolve
  its display through Azure location metadata, and use registry/config values only as cross-checks.
  Never hardcode a renderer fallback or add multi-region behavior.
- Validation deploys 41 independent endpoints: five baselines plus all 36 issues.
- Baselines require `5/5` healthy attempts. Deterministic issues require `5/5` with paired `v0`
  `0/5`; model-mediated issues require `5/7` with paired `v0` `0/7`.
- Validation mode is reviewed catalog data bound into `execution_digest`; never infer or reclassify
  it from runtime results, resample a miss, or lower `n`/`k`.
- A visible Copilot coordinator publishes immutable assignments, releases its lock, remains
  responsive, and creates all parallel validation sub-sessions. Never use subprocesses,
  `ThreadPoolExecutor`, or another hidden in-process pool for deployment, invocation, or verification.
- Deployment, invocation, and verification each independently publish one to eight deterministic,
  cost-balanced logical shards based on selected authorities. Each active shard maps 1:1 to one
  visible sub-session; eight is the per-phase shard and active-concurrency ceiling.
- The coordinator runs `prepare-test-agent-validation`, gives each sub-session exactly one assigned
  deploy/invoke/verify shard primitive, runs deployment reconciliation and final composition itself,
  and uses `run-test-agent-validation` only for status/next-action guidance.
- Shard primitives accept only the immutable shard ID. Never pass run/generation IDs or authority IDs;
  each primitive resolves the hidden active generation and assignment.
- Immediately after definitive authority completion, atomically publish a generation-fenced
  invocation receipt. Bind exact Agent source/content/execution, provider-version, runtime,
  environment, Project, telemetry resource-set, response/session, invoke/evidence-window,
  source-artifact schema/version/origin/digest, and complete issue/paired-`v0` provenance.
- Reject unknown, ambiguous, duplicate, partial, or indeterminate retried-POST outcomes. Cross-run
  receipt extraction is one-time, atomic, and fenced against stale sub-sessions.
- A reused receipt proves the traffic-generation/execution binding only. Every new verification
  package binds the receipt digest and current verifier commit/digest.
- Verification uses at most eight visible sub-sessions. Each claims one authority at a time, runs no
  internal concurrency, deploy, or traffic, and persists one immutable generation-fenced result
  before claiming another.
- Query telemetry once per target as a batched stability snapshot: one baseline batch, or two issue
  batches covering the issue and paired `v0`. Never stabilize attempts independently.
- Keep authority `PASS`, `FAIL`, and `INCOMPLETE` distinct. Apply baseline `5/5`, deterministic
  `5/5` plus paired `v0` `0/5`, and model-mediated `>=5/7` plus paired `v0` `0/7`.
- A later failure never discards completed authority results. Retry only missing, `INCOMPLETE`, or
  exact-binding-changed authorities; unchanged definitive `PASS` and `FAIL` results are reusable.
- A later generation selects only changed, incomplete, or missing authorities. Invoke only those
  without current exact-bound completed receipts; assign the rest verify-only work with no new
  endpoint traffic.
- Stale sub-sessions fail closed. Validation has no cleanup, and final composition requires exact
  fresh or reusable evidence for all 41 authorities.
- Validation is report-free: never create monitors, run Agent Insights, assess/score cards, publish
  ADX, send email, or run Daily from Test Agent Validation.
- Every potentially long-running operation emits public-safe start, elapsed heartbeat, and
  completion/failure progress. Progress-output failures must never abort the operation.
- `g30` is the active Sweden Central telemetry topology: one App Insights and Log Analytics pair per
  profile with Project-scoped App Insights connections. The West US 2 `g29` topology remains
  untouched until a later reviewed retirement.
- Run generation is opaque, hidden, and system-generated; never accept it as operator input or encode
  manual revision, generation, or recovery suffixes in Project or Agent names.
- The telemetry resource set is not an Agent deployment change and must not invalidate promotion
  receipts or Agent content digests.
- Monitor reset does not delete telemetry. Do not impose an unconditional pre-traffic sleep; bind
  evidence to exact run, Agent, provider version, operation, and time-window identities, then wait
  within the bounded post-invoke hydration and stability deadline.
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
- `correct_over_expected_plus_noise_v1` is `100 * correct issues / (expected issues + noise cards +
  duplicate cards)`. Title, description, category, and linked traces determine correctness; severity
  and proposed fix are diagnostic only.
- Official Daily builds a public-safe normalized improvement input after assessments. GPT-5.6 Sol
  returns only the strict improvement-analysis schema; deterministic code reconciles living pattern
  state. Only `insight_engine` ownership is eligible, and the result is advisory and score-neutral.
- Publish `reports/insight-engine-improvement.{json,md}` and the immutable dated snapshot with the
  Daily report. Email-only tests write a private preview only and never link or mutate living memory.
- Complete runs publish one numeric score with no PASS/FAIL label or threshold. Incomplete evidence
  fails internally and produces no report, email, ADX row, trend point, pull request, or promotion
  receipt.

## Change workflow

1. Make the smallest coherent change.
2. Generate readable catalog views when a catalog changes.
3. Run repository validation, Ruff, and tests.
4. Compile Bicep when infrastructure changes.
5. Freeze one clean commit after exactly one comprehensive review and targeted mechanical
   verification, then run local Test Agent Validation.
6. Keep atomic lifecycle, history, desired state, receipts, registries, and evidence under the
   environment-namespaced durable runtime root. Retain the durable Project, Agent topology, sessions,
   responses, images, telemetry, and evidence; supersede incomplete local state without deletion.
7. After explicit human approval, create the single minimal create-once approved validation record.
   GitHub has no validation gate and merge remains manual.
8. Preserve the legacy West US 2 resources and lifecycle state unchanged. The Sweden staging gate is
   the only active validation path and is never a fallback to the legacy environment.

```powershell
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m ruff check .
python -m pytest
az bicep build --file infra\main.bicep --stdout
```

## Skills

- `.github/skills/agent-insights-quality-daily/SKILL.md`: weekday qualification and publication.
- `.github/skills/test-agent-validation/SKILL.md`: local 41-authority validation and approval.
- `.github/skills/staging-qualification/SKILL.md`: durable Sweden Central staging qualification.
- `.github/skills/onboard-test-agent/SKILL.md`: add one reviewed fixed Test Agent.
- `.github/skills/onboard-new-issue/SKILL.md`: add one reviewed issue.
