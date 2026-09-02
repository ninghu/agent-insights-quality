# Contributing

Agent and Issue catalogs, Agent implementations, schemas, score policy, infrastructure, and promotion
receipts are human-reviewed contracts. Keep every change synthetic and public-safe.

## Review the existing catalogs

Before proposing a Test Agent or issue, review:

- the generated [Agent Catalog](AGENT_CATALOG.md) for current Agents, owners, frameworks, and issue
  counts;
- the generated [Issue Catalog](ISSUE_CATALOG.md) for existing reviewed defects and their permanent
  Agent assignments.

The source authorities are [`catalogs/AGENT_CATALOG.yaml`](catalogs/AGENT_CATALOG.yaml) and
[`catalogs/ISSUE_CATALOG.yaml`](catalogs/ISSUE_CATALOG.yaml). Update the YAML catalogs and regenerate
the readable views rather than editing the generated Markdown directly.

## Test Agent and issue contract

Each permanent Test Agent owns one healthy baseline and one or more reviewed issue versions:

- `v0` is a complete, deployable healthy version and must not contain an intentional defect.
- Every non-baseline logical version represents exactly one `issue-NNN`, and each issue is assigned
  exactly once to one permanent Test Agent in both catalogs.
- Each issue version contains exactly one independently fixable, reviewed root cause and remains
  complete and independently deployable.
- Deterministic synthetic traffic must exercise the deployed version through its Agent endpoint so
  telemetry is produced naturally.
- Every authority has versioned setup/probe scenarios. Classify the observation mechanism explicitly
  as `baseline`, `deterministic`, or `model_mediated`; never derive it from Prompt versus Hosted type.
- Preserve the fixed validation matrices: baseline `5/5`, deterministic issue `5/5` with paired `v0`
  `0/5`, and model-mediated issue `5/7` with paired `v0` `0/7`.

## Use the onboarding skills

Do not invent an onboarding workflow from scratch. Ask Copilot to follow the appropriate versioned
repository skill:

- Test Agent:
  [`.github/skills/onboard-test-agent/SKILL.md`](.github/skills/onboard-test-agent/SKILL.md)
- Issue: [`.github/skills/onboard-new-issue/SKILL.md`](.github/skills/onboard-new-issue/SKILL.md)

```text
Use the onboard-test-agent skill to propose a new fixed synthetic Test Agent. Stop for human
review of its name, framework, baseline contract, owner, and issue set before implementation.
```

```text
Use the onboard-new-issue skill to propose issue-NNN for <agent>. Stop for human review before
changing catalogs or Agent source.
```

Both skills begin with a reviewable plan and stop for human review before changing catalogs, source,
or runtime state. The `onboard-test-agent` skill composes `onboard-new-issue` for every initial issue;
use `onboard-new-issue` directly when adding an issue to an existing Agent.

## Onboard a new Test Agent

The `onboard-test-agent` skill is authoritative. A new permanent Agent requires:

- a reviewed stable name, Foundry type, real framework, model, owner, and healthy baseline contract;
- a complete deployable `v0` implementation with at least five deterministic healthy requests;
- at least one reviewed single-root issue created through `onboard-new-issue`;
- updates to both catalogs and every derived topology, schema, CI, reporting, and documentation
  contract.

Use the real framework packaging required by the Agent type. Do not add a placeholder implementation,
private endpoint, compatibility alias, shared state with another Agent, or synthetic trace injection.

## Onboard a new issue

The `onboard-new-issue` skill is authoritative. A new issue requires:

- the next continuous `issue-NNN` ID, one permanent Agent assignment, and a reviewed expected title,
  root cause, category, severity, proposed fix, and natural trace contract;
- a complete self-contained Prompt `definition.json` or Hosted `source/` tree containing exactly one
  independently fixable root cause;
- deterministic synthetic endpoint traffic that exercises the defect while `v0` remains healthy;
- a reviewed source-delta contract and deterministic activation assertions that distinguish the issue
  from its updated healthy baseline;
- matching catalog entries and updates to every derived topology, schema, CI, reporting, promotion,
  test, skill, and readable-document contract.

Do not use runtime mode switches, dormant defect branches, generic hooks, source patches, telemetry
injection, compatibility aliases, or private data.

Prompt Test Agents are pure Prompt contracts. Do not add function tools or traffic tool fixtures;
multi-turn traffic may use response continuation only for an intentional conversation-memory check.

## Validate the change

Install the development dependencies once:

```powershell
python -m pip install -e ".[dev]"
```

When a catalog changes, regenerate its readable views before validation:

```powershell
python -m agent_insights_quality generate-docs
```

Run these checks for every change:

```powershell
python -m agent_insights_quality validate
python -m ruff check .
python -m pytest
```

Compile Bicep only when infrastructure changes:

```powershell
az bicep build --file infra\main.bicep --stdout
```

## Validate a clean commit

Follow the [Test Agent Validation skill](.github/skills/test-agent-validation/SKILL.md). Validation
auto-discovers one clean PR-head commit. A visible Copilot coordinator publishes immutable assignments
and creates the deployment, invocation, and verification sub-sessions; do not substitute subprocesses,
`ThreadPoolExecutor`, or another in-process pool. Each phase independently publishes one to eight
deterministic, cost-balanced logical shards, with every active shard mapped 1:1 to one visible
sub-session.

The coordinator runs `prepare-test-agent-validation`, assigns exactly one deploy/invoke/verify
primitive to each sub-session, and runs reconciliation and composition itself. Shard commands accept
only the immutable shard ID and resolve the hidden active generation and authority assignment.
`run-test-agent-validation` is status/next-action guidance only and never creates sub-sessions.

Immediately after definitive authority completion, its sub-session atomically publishes a
generation-fenced invocation receipt. Cross-generation or verifier-only reuse requires exact Agent
source/content/execution, provider-version, runtime, environment, Project, telemetry resource-set,
response/session, invoke/evidence-window, source-artifact schema/version/origin/digest, and complete
issue/paired-`v0` provenance. Unknown, ambiguous, duplicate, partial, or indeterminate retried-POST
outcomes are not reusable, and one-time extraction is fenced against stale sub-sessions.

The next generation selects only changed, failed, incomplete, or missing authorities; within that
set, only authorities without current exact-bound completed receipts receive new traffic. All others
use verify-only recovery. A reused receipt proves the traffic-generation/execution binding, while
each new verification package binds its immutable digest and the current verifier commit/digest. No
validation cleanup runs, and the composed READY or FAILED result always covers all 41 authorities.

The fixed daily contract selects four issues per Agent: 20 issues plus five baselines, for 25
assessment packages. Test Agent Validation is a separate local report-free process and never runs Agent
Insights, assessment, scoring, ADX publication, or email.

After exact current-head 41/41 READY evidence, explicit human approval permits one minimal create-once
approved validation record. GitHub runs ordinary mechanical CI only and merge remains manual. This
durable `aiq-staging-swedencentral` process is the official staging qualification gate for new
candidates.

Local runtime prerequisites and operator roles are documented in
[`docs/AUTOMATION_SETUP.md`](docs/AUTOMATION_SETUP.md).
