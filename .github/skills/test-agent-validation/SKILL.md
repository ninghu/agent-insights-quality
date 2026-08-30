---
name: test-agent-validation
description: Validate all fixed Test Agent authorities through the protected candidate merge gate.
license: MIT
---

# Test Agent Validation

Use this skill for the report-free candidate gate. It never runs Agent Insights, assessment, scoring,
report publication, ADX, email, or Official Daily.

1. Read the trusted policy and both reviewed catalogs. Regenerate rules and readable views, then run
   repository validation, Ruff, tests, hosted builds, and Bicep compilation.
2. Freeze every authority's reviewed mode before traffic. Baselines are `5/5`; deterministic issues
   are `5/5` with paired `v0` `0/5`; model-mediated issues are `5/7` with paired `v0` `0/7`.
   Never infer mode from runtime kind, resample, reclassify from results, or lower a threshold.
3. Acquire the account-wide infinite Blob lease. Every journal mutation must use that lease and the
   current ETag, write an immutable event first, and retain the fixed 72-hour absolute expiration.
4. Measure RPM/TPM and the complete outer/inner request envelope. Preserve the reviewed percentage
   and absolute headroom. Provision at no more than eight, query telemetry at no more than four, and
   allow one scenario attempt per runtime.
5. Create exactly one opaque temporary Project and 41 independently named Agent endpoints. Record
   intent before every create. Hosted and Container code must fail closed without authoritative
   `FOUNDRY_AGENT_NAME` and `FOUNDRY_AGENT_VERSION`.
6. Run every fixed setup/probe attempt through deployed endpoints. Use the identical issue matrix
   against its paired `v0`. Read staging `g29` telemetry only; ambiguous identity or correlation
   aborts. Application Insights remains read-only. Do not create monitors or inject traces.
7. Freeze the scope after complete 41/41 evidence. Exactly one comprehensive review covers that
   frozen scope. On the final head, run only targeted finding verification plus CI. A shared or
   architecture change aborts and starts a new cycle.
8. Write cleanup intent, delete every recorded response, conversation/session, Agent/version, Hosted
   deployment/identity/blueprint, connection, role assignment, cycle principal, tag, and unshared
   manifest in reverse dependency order, then delete the Project. Verify Project `404`, no nonce-owned
   resource, no session/response, no cycle tag, and every reviewed cascade.
9. If cleanup is ambiguous, enter `CLEANUP_BLOCKED`, keep the account unavailable, and let only the
   reconciler take over with a fresh lease ID, nonce, and epoch. Never replace the Project in-cycle.
10. A shadow receipt binds candidate-head policy and explicitly sets
    `default_branch_trust_anchor_present=false` and `authorizes_merge=false`. A merge receipt is
    create-once and requires default-branch policy/workflow/App/environment provenance, exact final
    head, one review, targeted verification, CI, 41/41 evidence, and immutable `CLEAN` proof.

Keep all lifecycle/evidence/receipt artifacts in private Blob storage and the durable user runtime
root. Never put raw prompts, responses, traces, provider IDs, Azure IDs, or private context in Git.
