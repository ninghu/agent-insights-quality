---
name: staging-qualification
description: Run full-catalog staging qualification and reviewed promotion to daily.
license: MIT
---

# Staging Qualification

Use this skill for human-reviewed impact-based qualification before promotion.

1. Sync the latest default branch before changing contracts or infrastructure.
2. Validate catalogs, generated docs, schemas, tests, Ruff, and Bicep.
3. Reuse and verify the fixed `g29` telemetry resource set and both Foundry Project connections.
4. Provision staging, reconcile exactly 41 versions and five monitors, and publish the canonical
   staging registry to the private Azure Blob container.
5. Fetch the private Quality-tagged work-item snapshot under the durable user-level runtime root.
6. Wait for the configured `0.1`-hour clean window for all five Agents.
7. Determine affected Agents from content digests, mappings, and shared-contract changes:
   - qualify each affected Agent's `v0` and all assigned issues;
   - reuse reviewed evidence for unchanged Agents only when digest, mapping, and shared contracts are
     unchanged;
   - use full-catalog qualification for shared runtime, telemetry, assessment, scoring, schema,
     infrastructure, or cross-Agent topology changes.
8. Require one complete package for every selected baseline and issue.
9. Assess selected baselines and issues with GPT-5.6 Sol. Equal nonzero request, response, and usable
   response counts plus a verified trace contract prove the reviewed runtime contract was exercised.
   Semantic assertions are optional corroboration.
   Independently verify every baseline card; `v0` is a reviewed healthy contract, not proof that its
   runtime behavior was healthy.
10. Before finalization, give every `inconclusive` baseline or `INCOMPLETE` issue with complete runtime
    evidence one focused GPT-5.6 Sol recheck. Re-read the package-bound reviewed Agent source and
    configuration, current endpoint evidence, independent trace proof, and the card's exact claim.
    Never send new traffic for this recheck, never force a conclusive verdict, and retain `INCOMPLETE`
    when independent evidence remains insufficient.
11. Finalize with `--work-items <snapshot>` and inspect score, noise, ownership, and every non-matched
    finding. Any
   `inconclusive` baseline assessment or `INCOMPLETE` issue assessment makes the whole run
   `INCOMPLETE` with no numeric quality score. Never commit the snapshot.
   Open the private `report-preview.html` in a browser for human review. It uses the exact same
   Outlook-safe HTML renderer and private quality-trend dashboard link as the daily email; never
   commit the preview, dashboard link, or its private context.
12. After explicit human review, compose promotion from complete trusted PASS/FAIL evidence for
    affected Agents and latest valid receipts for unchanged Agents. Verify every exact digest and
    mapping. Never promote or reuse `INCOMPLETE`.

Use only reviewed tooling that validates targeted reports and composed receipts. If the current CLI
cannot produce both, stop targeted execution and use full-catalog qualification; never splice
evidence or receipts manually.

If a run is incomplete, preserve its artifacts, inspect stage-specific error codes, and resume only
the failing stage when its private checkpoint proves prior work complete. Routine recovery reuses
`g29`; never create a telemetry resource for a rerun.

After exact-digest daily provisioning, perform read-only readiness and registry reconciliation; do
not send daily smoke traffic. Do not promote `INCOMPLETE`, overlap the short clean window, weaken
an Issue contract to make a run pass, or persist private runtime identifiers.
