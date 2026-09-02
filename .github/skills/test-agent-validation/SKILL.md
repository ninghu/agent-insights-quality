---
name: test-agent-validation
description: Validate fixed Test Agent authorities locally for explicit human approval.
license: MIT
---

# Test Agent Validation

Use this skill for the report-free Sweden Central staging gate. It never runs Agent Insights,
assessment, scoring, reporting, ADX, email, Daily traffic, approval, or merge.

1. Freeze one reviewed clean commit and run:

   ```powershell
   python -m agent_insights_quality run-test-agent-validation
   ```

   The command discovers the exact repository, PR head, durable
   `aiq-staging-swedencentral` Account and Project, and private runtime root. Its opaque generation
   identity is system-created and is never supplied through the CLI.
2. Treat every catalog authority as one unique runtime Agent identity and one indivisible
   deployment assignment. The coordinator computes exact source and provider-content digests,
   publishes an immutable desired-state plan, then releases the coordinator lock.
3. Distribute changed authorities exactly once across no more than the reviewed provisioning
   concurrency (currently eight) asynchronous workers. Each worker owns disjoint authority locks,
   may exact-reuse or deploy only its assigned version, and writes immutable per-authority readiness
   receipts. Workers never write shared lifecycle, topology, or registry state.
4. After the deployment barrier, centrally re-read all 41 exact versions, verify the durable
   Project, read-only Sweden `g30` telemetry binding, and zero-monitor invariant, then atomically
   publish one reconciled topology and deployment registry; never select `latest`.
5. Validate only authorities whose content changed, whose latest result is FAIL or incomplete, or
   that lack exact-bound PASS evidence. Reuse PASS evidence only when source/content digests,
   server-assigned version, runtime mapping, environment, and shared validation contracts match.
   Every selected issue receives a fresh paired `v0` control.
6. Invoke selected authorities with at most eight asynchronous workers. After invocation completes,
   verify exact response-bound traces with at most four workers and no new traffic. A response must
   map one-to-one to its exact `invoke_agent` anchor and complete descendant span tree; sibling roots
   cannot contribute evidence.
7. Composition requires every selected receipt plus exact reusable evidence for all other
   authorities. It transitions to `READY` only for exact 41-authority PASS evidence and otherwise
   records `FAILED`. Missing, ambiguous, stale, orphaned, cyclic, duplicate, or conflicting bindings
   fail closed and produce no package.
8. Starting a new validation writes an immutable `SUPERSEDED` event and atomically swaps active
   state. It never deletes provider sessions, responses, Agents, versions, identities, deployments,
   images, telemetry, registries, or evidence. A legacy active record is archived byte-for-byte and
   referenced only by an opaque tombstone digest.
9. Stop for human review of the exact READY evidence. Only after explicit approval may
   `approve-test-agent-validation` create the minimal immutable Sweden `g30` record. Daily promotion
   remains separate and sends no smoke traffic.

Keep lifecycle, desired state, fine-grained locks, receipts, invocation bindings, packages, and
evidence under the private durable
`~/.aiq-runtime/agent-insights-quality/test-agent-validation/` root. Never write credentials,
private Azure identifiers, raw traces, complete payloads, or private context to Git. The preserved
West US 2 environment and lifecycle are never modified.
