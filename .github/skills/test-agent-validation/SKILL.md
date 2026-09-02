---
name: test-agent-validation
description: Validate fixed Test Agent authorities locally for explicit human approval.
license: MIT
---

# Test Agent Validation

Use this skill for the report-free Sweden Central staging gate. It never runs Agent Insights,
assessment, scoring, reporting, ADX, email, Daily traffic, approval, or merge.

1. Freeze one reviewed clean commit. The visible Copilot coordinator runs:

   ```powershell
   python -m agent_insights_quality prepare-test-agent-validation
   ```

   The command discovers the exact repository, PR head, durable
   `aiq-staging-swedencentral` Account and Project, and private runtime root. Its opaque generation
   identity is hidden, system-created, never supplied through the CLI, and never encoded in Project
   or Agent names.
2. Treat every catalog authority as one unique runtime Agent identity and one indivisible
   deployment assignment. The coordinator computes exact source and provider-content digests,
   publishes immutable desired-state and phase assignments, then releases the coordinator lock and
   remains responsive.
3. The coordinator creates visible Copilot sub-sessions for all parallel deployment, invocation, and
   verification work. Never use subprocesses, `ThreadPoolExecutor`, or any other hidden in-process
   pool. Each non-empty phase independently publishes one to eight deterministic, cost-balanced
   logical shards based only on its selected authorities. Every active shard maps 1:1 to one visible
   sub-session; the per-phase ceiling is currently eight.
4. Distribute changed deployment authorities exactly once. Each deployment sub-session owns a
   disjoint immutable assignment and authority locks, may exact-reuse or deploy only its assigned
   version, and writes immutable per-authority readiness receipts. Sub-sessions never write shared
   lifecycle, topology, or registry state; stale sub-sessions fail closed. Give every sub-session
   exactly one command:

   ```powershell
   python -m agent_insights_quality deploy-test-agent-validation-shard --shard-id <N>
   ```

5. After the deployment barrier, the coordinator runs:

   ```powershell
   python -m agent_insights_quality reconcile-test-agent-validation-deployment
   ```

   Reconciliation centrally re-reads all 41 exact versions, verifies the durable Project, read-only
   Sweden `g30` telemetry binding, and zero-monitor invariant, then atomically publishes one topology,
   deployment registry, and the selected invocation and verification assignment sets; never select
   `latest`.
6. Select only authorities whose Agent source, provider content, or execution contract changed,
   whose latest result is FAIL or incomplete, or whose exact evidence is missing. Every authority
   selected for new issue traffic receives a fresh paired `v0` control.
7. Give each invocation sub-session exactly one command:

   ```powershell
   python -m agent_insights_quality invoke-test-agent-validation-shard --shard-id <N>
   ```

8. Immediately after definitive authority completion, atomically publish its generation-fenced
   invocation receipt instead of waiting for the shard to finish. Bind the exact Agent source digest,
   provider-content digest, traffic-generation/execution digest, server-assigned provider version,
   runtime mapping, environment, Project, telemetry resource-set identity, response/session references,
   invoke/evidence windows, complete issue and paired-`v0` provenance, source-artifact schema/version,
   origin run/commit/generation/shard, and immutable artifact digest.
   Unknown, ambiguous, duplicate, partial, or indeterminate retried-POST outcomes are not reusable.
   Cross-generation reuse performs one atomic, generation-fenced extraction; stale sub-sessions
   cannot extract or publish the receipt.
   A new generation selects only changed, failed, incomplete, or missing authorities. Within that set,
   invoke only authorities without current exact-bound completed receipts; assign all others
   verify-only work and send no new endpoint traffic.
9. Begin verification only after the invocation barrier. Verification is read-only and never sends
   traffic. Give every verification sub-session exactly one command:

   ```powershell
   python -m agent_insights_quality verify-test-agent-validation-shard --shard-id <N>
   ```

   Response-bound traces must map one-to-one to the exact `invoke_agent` anchor and complete descendant
   span tree; sibling roots cannot contribute evidence. Receipt reuse proves the unchanged
   traffic-generation and execution binding only. Every new verification package binds the reused
   receipt's immutable digest and the current verifier commit and verifier digest.
10. After the verification barrier, the coordinator runs:

    ```powershell
    python -m agent_insights_quality compose-test-agent-validation
    ```

    Composition requires every selected fresh receipt plus exact reusable evidence for all other
    authorities. It transitions to `READY` only for exact 41-authority PASS evidence and otherwise
    records `FAILED`. Missing, ambiguous, stale, orphaned, cyclic, duplicate, conflicting, or
    cross-root bindings fail closed and produce no package.
11. Starting a new validation writes an immutable `SUPERSEDED` event and atomically swaps active
    state. Validation has no cleanup and never deletes provider sessions, responses, Agents, versions,
    identities, deployments, images, telemetry, registries, receipts, or evidence. A legacy active
    record is archived byte-for-byte and referenced only by an opaque tombstone digest.
12. Stop for human review of the exact READY evidence. Only after explicit approval may
    `approve-test-agent-validation` create the minimal immutable Sweden `g30` record. Daily promotion
    remains separate and sends no smoke traffic.

Shard primitives accept only `--shard-id`; they never accept run/generation IDs or authority IDs.
Each resolves the hidden active generation and exact immutable assignment. Use
`run-test-agent-validation` only to read status and next-action guidance. It never creates sub-sessions
or executes phase work.

Keep lifecycle, desired state, fine-grained locks, receipts, invocation bindings, packages, and
evidence under the private durable
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/` root. Never write credentials,
private Azure identifiers, raw traces, complete payloads, or private context to Git. The preserved
West US 2 environment and lifecycle are never modified.
