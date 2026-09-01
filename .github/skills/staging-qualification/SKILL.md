---
name: staging-qualification
description: Run the durable Sweden Central staging qualification for a reviewed candidate.
license: MIT
---

# Sweden Central Staging Qualification

Use this skill for the official human-reviewed gate before Daily Agent promotion. The executable
workflow is `.github/skills/test-agent-validation/SKILL.md`; follow that complete contract.

1. Freeze one comprehensively reviewed clean commit and run repository validation, generated-doc
   checks, Ruff, tests, and Bicep compilation before cloud mutation.
2. Bind the exact existing `aiq-staging-swedencentral` Account and identically named durable Project.
   Verify the Sweden Central `g30` Project-scoped Application Insights connection and the exact
   `gpt-5.4-mini` `2026-03-17` DataZoneStandard deployment. Never create or delete the Project.
3. Hold the environment-namespaced OS lock and keep lifecycle, history, evidence, and CLEAN state
   under `~/.aiq-runtime/agent-insights-quality/test-agent-validation/`.
4. Reconcile all five baselines and 36 issues as 41 stable baseline/issue Agent names. Persist and
   exact-select each server-assigned provider version ID and content digest; never route to a floating
   version.
5. Invoke only the deployed endpoints. Run the five Agent lanes concurrently, versions sequentially
   within each Agent, and every issue against its paired `v0` using the catalog-frozen attempt matrix.
6. Keep Application Insights read-only. Correlate exact run, Agent, provider version, operation, and
   invocation time-window identities, then wait only within the bounded post-invoke hydration and
   stability deadline. Missing attributable traces fail closed.
7. Validation remains report-free: create no monitor and do not run Agent Insights, assessment,
   scoring, reporting, ADX publication, email, or Daily.
8. On failure or commit drift, clean only run-scoped resources. Retain the durable Project and stable
   Agent/version topology, and never mutate the preserved West US 2 lifecycle or resources.
9. After exact 41/41 evidence and CLEAN proof, stop for explicit human review. Only after approval may
   `approve-test-agent-validation` create the minimal immutable approved record for the exact commit.
10. Daily may then provision only exact approved digests into `aiq-daily-swedencentral`. Perform
    read-only readiness and registry reconciliation and send no Daily smoke traffic.

Never write credentials, private Azure identifiers, raw traces, complete payloads, or private
work-item context to Git. The old West US 2 environment is never a staging fallback and remains
untouched until a separate reviewed retirement.
