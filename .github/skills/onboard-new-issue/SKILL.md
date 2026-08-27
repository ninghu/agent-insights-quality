---
name: onboard-new-issue
description: Add one human-reviewed single-root issue to an existing or newly proposed Test Agent.
license: MIT
---

# Onboard New Issue

Add one independently fixable issue only after human review.

1. Produce a reviewable plan and stop for human approval before changing catalogs or source.
2. Choose the existing permanent Agent, or the proposed Agent in an enclosing new-Agent migration,
   based on its real Foundry type and implementation surface.
3. Allocate the next continuous `issue-NNN` ID.
4. Add one Issue Catalog entry with expected title, root cause, category, severity, proposed fix, and
   one natural trace contract.
5. Append the new ID to the permanent Agent's ordered `issue_ids` in
   `catalogs/AGENT_CATALOG.yaml`; both catalogs must agree exactly.
6. Add `agents/<agent>/issues/issue-NNN/implementation.yaml` and deterministic `traffic.json` requests
   that exercise the reviewed defect.
7. For Prompt Agents, add a complete deployable `definition.json`.
8. For Hosted Agents, copy the complete healthy `source/` tree into the issue folder and modify only
   the reviewed defect location. Do not use runtime mode switches, dormant defect branches, hooks, or
   build-time patches.
9. Prove `v0` remains a healthy static contract and the issue implementation differs by exactly one
   reviewed root. Propagate unrelated baseline maintenance into every self-contained version.
10. Generate docs, validate, and test deterministic packaging.
11. Search for the previous Agent count, issue count, version count, final issue ID, and explicit
   Agent-name lists. Update every matching schema, runtime validator, promotion check, CI matrix,
   test, skill, generated view, and readable document, including promotion/deployment registry schemas
   and generated-change validation.
12. Confirm the Agent has at least one assigned issue and daily selection uses
   `min(5, assigned issues)`.
13. Run repository validation, Ruff, tests, and Bicep compilation.
14. Deploy the affected Agent digests to staging, qualify that Agent's `v0` and all assigned issues,
    complete Sol assessment, and require human review before daily promotion. Reuse other Agents'
    evidence only when their digests, mappings, and shared contracts are unchanged. In an enclosing
    new-Agent migration, catalog assignment, qualification, and promotion may be completed atomically
    by the parent.
15. Compose promotion from the affected Agent's complete reviewed PASS/FAIL evidence plus valid
    receipts for unchanged Agents. Require matching mappings and every exact digest. Never promote or
    reuse INCOMPLETE.

Never add a multi-root issue, compatibility alias, telemetry injection, or private data.
