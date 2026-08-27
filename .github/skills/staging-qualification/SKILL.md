---
name: staging-qualification
description: Run full-catalog staging qualification and reviewed promotion to daily.
license: MIT
---

# Staging Qualification

Use this skill for a human-reviewed full qualification before promotion.

1. Sync the latest default branch before changing contracts or infrastructure.
2. Validate catalogs, generated docs, schemas, tests, Ruff, and Bicep.
3. Deploy the reviewed telemetry generation and verify both Foundry Project data planes.
4. Provision staging, reconcile exactly 41 versions and five monitors, and publish the canonical
   staging registry to the private Azure Blob container.
5. Fetch the private Quality-tagged work-item snapshot under the durable user-level runtime root.
6. Verify a clean three-hour window for all five Agents.
7. Run `python -m agent_insights_quality run-full --report-date <Pacific date> --work-items <snapshot>`.
   The runner must synchronize the canonical staging registry before traffic.
8. Require 41 private assessment packages with complete runtime evidence.
9. Assess five baselines and 36 issues with GPT-5.6 Sol. Equal nonzero request, response, and usable
   response counts plus a verified trace contract prove the reviewed runtime contract was exercised.
   Semantic assertions are optional corroboration.
   Independently verify every baseline card; `v0` is a reviewed healthy contract, not proof that its
   runtime behavior was healthy.
10. Finalize with `--work-items <snapshot>` and inspect score, noise, ownership, and every non-matched
    finding. Any
   `inconclusive` baseline assessment or `INCOMPLETE` issue assessment makes the whole run
   `INCOMPLETE` with no numeric quality score. Never commit the snapshot.
11. Create a promotion receipt for a complete, trusted `PASS` or `FAIL` report after explicit human
    review. Never promote `INCOMPLETE`.

If a run is incomplete, preserve its artifacts, inspect stage-specific error codes, and reproduce only
the failing stage. Rotate telemetry only after fixing the root cause: deploy and validate the new
generation before deleting the exact old App Insights and workspace set.

Do not promote `INCOMPLETE`, reuse a dirty telemetry generation, weaken an Issue contract to make a
run pass, or persist private runtime identifiers.
