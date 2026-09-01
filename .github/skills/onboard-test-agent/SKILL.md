---
name: onboard-test-agent
description: Add one fixed synthetic Test Agent and its initial reviewed issue set.
license: MIT
---

# Onboard Test Agent

Add one new synthetic Test Agent only as a human-reviewed fixed-topology migration.

1. Produce a reviewable plan and stop for human approval before any repository or runtime mutation.
   The plan must include the Agent contract and every proposed issue's ID, title, root cause, category,
   severity, expected fix, trace contract, deterministic traffic case, and defect location.
2. Approve the stable Agent name, Foundry type, real framework, model, baseline behavior, and
   maintenance owner. Require explicit acceptance from that owner or an authorized maintainer.
3. Add a complete `agents/<agent>/v0` implementation and at least five deterministic healthy traffic
   requests.
4. Add at least one independently fixable, single-root issue. For every initial issue, follow every
   step in `.github/skills/onboard-new-issue/SKILL.md` and assign the resulting ordered issue ID to the
   new Agent. Every Prompt issue owns a complete `definition.json`; every Hosted issue owns a complete
   `source/` tree containing only its defect. Issue traffic must exercise the reviewed defect
   deterministically.
5. Add the Agent, owner, and permanent issue assignments to both reviewed catalogs.
6. Derive `agent_count`, `validation_issue_count`,
   `daily_issue_count = sum(min(4, assigned_issue_count))`, and
   `authority_count = agent_count + validation_issue_count`, then update every fixed-count contract:
   - Agent and Issue catalog schemas;
   - run-manifest and report schemas;
   - Daily expected counts, validation topology, and score denominator;
   - catalog, selection, reporting, promotion, and deployment tests;
   - generated Agent and Issue catalog views;
   - README, Operations, Quality Bar, and CONTRIBUTING;
   - hosted Agent import/build CI matrix when applicable.
   - promotion/deployment registry schemas and checks, runtime validators, explicit Agent-name
    allowlists, skills, and generated-change workflow constraints.
   - daily selection and report validation so each Agent contributes `min(4, assigned issues)`.
7. For Hosted code, include `implementation.yaml`, `traffic.json`, `source/`, `host.yaml`,
   `requirements.txt`, and deterministic `package.py`; include the container contract and CI build
   entry for Hosted containers.
8. Verify deterministic packaging, exact-version routing, endpoint behavior, natural telemetry,
   privacy-safe trace proof, and baseline ownership.
9. Run repository validation, Ruff, tests, and Bicep compilation.
10. Update each baseline and issue's fixed validation scenarios, explicit mode, `n/k`, paired-v0
   controls, execution digest, name limits, cleanup inventory, and exact authority count.
11. Run a fresh full Test Agent Validation cycle from one exact clean commit, require exact cleanup
   and explicit human approval, then create the single approved validation record.
   Validation remains report-free; never use the preserved old West US 2 environment as a fallback.

Use GPT-5.4 mini for the Test Agent and keep Agent Insights generation on its separate Terra
deployment. Do not add a placeholder implementation, private endpoint, compatibility alias, shared
state with another Agent, or synthetic trace injection.
