# Contributing

Agent and Issue catalogs, Agent implementations, schemas, score policy, infrastructure, and promotion
receipts are human-reviewed contracts. Keep every change synthetic and public-safe.

## Review the existing catalogs

Before proposing a Test Agent or issue, review the generated [Agent Catalog](AGENT_CATALOG.md) for
the current Agents and assignments, and the generated [Issue Catalog](ISSUE_CATALOG.md) for the
existing reviewed defects. Their source authorities are
[`catalogs/AGENT_CATALOG.yaml`](catalogs/AGENT_CATALOG.yaml) and
[`catalogs/ISSUE_CATALOG.yaml`](catalogs/ISSUE_CATALOG.yaml); update the YAML catalogs and regenerate
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

## Use the onboarding skills

Do not invent an onboarding workflow from scratch. Ask Copilot to follow the appropriate versioned
repository skill:

- New Test Agent:
  [`.github/skills/onboard-new-test-agent/SKILL.md`](.github/skills/onboard-new-test-agent/SKILL.md)
- New issue: [`.github/skills/onboard-new-issue/SKILL.md`](.github/skills/onboard-new-issue/SKILL.md)

Example prompts:

```text
Use the onboard-new-test-agent skill to propose a new fixed synthetic Test Agent. Stop for human
review of its name, framework, baseline contract, owner, and issue set before implementation.
```

```text
Use the onboard-new-issue skill to propose issue-NNN for <agent>. Stop for human review before
changing catalogs or Agent source.
```

Each skill must first produce a reviewable plan. Catalog, schema, source, infrastructure, score-policy,
and promotion changes cannot be approved solely by automation. The new-Test-Agent skill composes the
new-issue skill for every initial issue; use the new-issue skill directly when adding an issue to an
existing Agent.

## Onboard a new Test Agent

Use the new-Agent skill to:

1. review the stable Agent name, Foundry type, real framework, GPT-5.6 Terra model, owner, and healthy
   baseline contract, with explicit acceptance from that owner or an authorized maintainer;
2. add a complete deployable `v0` implementation and at least five healthy deterministic requests;
3. add at least one reviewed single-root issue with a self-contained implementation by following
   every step in the new-issue skill and assigning that issue to the new Agent;
4. update both catalogs and every Agent/Issue-count contract;
5. update CI build matrices, report schemas, score denominators, generated docs, and operator docs;
6. verify exact-version routing, endpoint behavior, natural telemetry, trace proof, and baseline
   ownership;
7. derive Agent, staging-issue, daily-issue, and total-version counts from the reviewed catalogs;
   daily selects `min(5, assigned issues)` for each Agent;
8. qualify the new Agent's baseline and all assigned issues when existing digests, mappings, and
   shared contracts are unchanged; otherwise qualify every affected Agent or the full catalog;
9. assess with GPT-5.6 Sol and require human review.

A Hosted-code Agent includes `implementation.yaml`, `traffic.json`, `source/`, `host.yaml`,
`requirements.txt`, and deterministic `package.py`; a Hosted-container Agent also includes its
container contract and CI build entry. Record the owner in the Agent Catalog.

`v0` source and configuration must be healthy. If runtime behavior violates that contract, classify
the evidence as `agent`, `test_framework`, `infrastructure`, or `unresolved`; never assume every
baseline card is Noise.

Keep the standalone new-issue skill. The new-Agent skill composes it for initial issues; contributors
also use it later to add issues to an existing Agent.

## Define a new issue

Use the new-issue skill to:

1. select one permanent Agent and one independently fixable root cause;
2. define the expected title, root cause, category, severity, proposed fix, and natural trace minimum;
3. allocate the next continuous `issue-NNN` identifier;
4. append that ID to the permanent Agent's ordered `issue_ids` in `catalogs/AGENT_CATALOG.yaml`;
5. create deterministic synthetic endpoint traffic that exercises the defect;
6. add a complete Prompt `definition.json` or Hosted `source/` tree containing only that defect;
7. prove `v0` remains a healthy static contract and the issue differs by exactly one reviewed root;
8. update all old topology counts, explicit Agent lists, schemas, runtime validators, promotion checks,
   CI matrices, tests, skills, generated views, and readable docs;
9. run full-catalog staging qualification and obtain human review before promotion.

Do not use runtime mode switches, dormant defect branches, generic hooks, source patches, telemetry
injection, compatibility aliases, or private data.

## Required validation

```powershell
python -m agent_insights_quality generate-docs
python -m agent_insights_quality validate
python -m ruff check .
python -m pytest
az bicep build --file infra\main.bicep --stdout
```

Agent-only changes qualify each affected Agent's baseline and all assigned issues. Unchanged Agents
reuse their latest reviewed evidence only when content digests, mappings, and shared contracts are
unchanged. Shared runtime, telemetry, assessment, scoring, schema, infrastructure, or cross-Agent
topology changes require full-catalog qualification or retained evidence re-evaluation when no new
traffic is needed. Compose promotion from complete reviewed PASS/FAIL evidence; INCOMPLETE is never
promotable or reusable. Daily provisioning ends with read-only readiness and registry reconciliation,
not smoke traffic.

Targeted qualification requires reviewed CLI support for both targeted reports and composed promotion
receipts. Until both are available, use full-catalog qualification and never combine evidence
manually.

Protected runtime prerequisites and operator roles are documented in
[`docs/AUTOMATION_SETUP.md`](docs/AUTOMATION_SETUP.md).
