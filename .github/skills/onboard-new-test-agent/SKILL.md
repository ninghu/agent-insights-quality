# Onboard New Test Agent

Add one new synthetic Test Agent only as a human-reviewed fixed-topology migration.

1. Approve the stable Agent name, Foundry type, real framework, model, baseline behavior, and
   maintenance owner.
2. Add a complete `agents/<agent>/v0` implementation and at least five deterministic healthy traffic
   requests.
3. Add at least five independently fixable, single-root issues so weekday selection can choose five
   issues for the Agent. Every Prompt issue owns a complete `definition.json`; every Hosted issue owns
   a complete `source/` tree containing only its defect.
4. Add the Agent and permanent issue assignments to both reviewed catalogs.
5. Update every fixed-count contract:
   - Agent and Issue catalog schemas;
   - run-manifest and report schemas;
   - daily and staging expected counts and score denominator;
   - catalog, selection, reporting, promotion, and deployment tests;
   - generated Agent and Issue catalog views;
   - README, Operations, Quality Bar, and CONTRIBUTING;
   - hosted Agent import/build CI matrix when applicable.
6. Verify deterministic packaging, exact-version routing, endpoint behavior, natural telemetry, and
   trace contracts.
7. Deploy to staging, run every Agent and issue, complete Sol assessment, and require human review
   before daily promotion.

Use GPT-5.6 Terra for the Test Agent and keep Agent Insights generation on its separate Terra
deployment. Do not add a placeholder implementation, private endpoint, compatibility alias, shared
state with another Agent, or synthetic trace injection.
