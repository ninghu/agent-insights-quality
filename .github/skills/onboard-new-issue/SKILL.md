# Onboard New Issue

Add one independently fixable issue only after human review.

1. Choose the permanent Agent based on its real Foundry type and implementation surface.
2. Allocate the next continuous `issue-NNN` ID.
3. Add one Issue Catalog entry with one expected Insight and one natural trace contract.
4. Add `agents/<agent>/issues/issue-NNN/implementation.yaml` and `traffic.json`.
5. For Prompt Agents, add a complete deployable `definition.json`.
6. For Hosted Agents, copy the complete healthy `source/` tree into the issue folder and modify only
   the reviewed defect location. Do not use runtime mode switches, dormant defect branches, hooks, or
   build-time patches.
7. Generate docs, validate, and test deterministic packaging.
8. Update fixed total-count contracts when the Issue Catalog size changes: Issue Catalog schema,
   staging report schema and validator, score denominator tests, promotion tests, generated docs, and
   readable documentation.
9. Confirm the permanent Agent still has at least five eligible issues for deterministic weekday
   selection.
10. Deploy the digest to staging and run full qualification before daily promotion.

Never add a multi-root issue, compatibility alias, telemetry injection, or private data.
