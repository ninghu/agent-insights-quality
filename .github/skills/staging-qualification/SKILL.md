---
name: staging-qualification
description: Run the durable Sweden Central staging qualification for a reviewed candidate.
license: MIT
---

# Sweden Central Staging Qualification

Use `.github/skills/test-agent-validation/SKILL.md` as the executable contract.

Prepare the one durable Sweden Central `g30` `aiq-staging-swedencentral` Project and all 41 unique catalog authorities before traffic.
Never create or delete the Project during validation.
The visible Copilot coordinator publishes immutable disjoint deployment assignments and independent
immutable invocation and verification assignments, releases its global lock, and remains responsive
while coordinator-created visible Copilot sub-sessions do parallel work. Never use subprocesses,
`ThreadPoolExecutor`, or another hidden in-process pool. Every non-empty phase independently publishes
one to eight deterministic, cost-balanced logical shards, at most eight, based on selected
authorities. Each active
shard maps 1:1 to one visible sub-session.

Only the coordinator may reconcile all exact versions and atomically publish shared topology and
registry state. It runs preparation, deployment reconciliation, and composition. It gives each
sub-session exactly one assigned deploy/invoke/verify shard command. Shard commands accept only the
immutable shard ID and resolve the hidden active generation and authority assignment.

Per-authority invocation receipts publish atomically and generation-fenced immediately after
definitive completion. The exact reusable contract includes Agent source/content/execution,
provider-version, runtime, environment, Project, telemetry resource-set, response/session,
invoke/evidence windows, complete issue/paired-`v0` provenance, source-artifact
schema/version/origin/digest, and an unambiguous completed POST outcome. Cross-generation extraction
is one-time and fenced against stale sub-sessions.

Completed current invocations support verify-only recovery with no new endpoint traffic. Verification
uses at most eight visible sub-sessions; each claims one authority at a time, uses no internal
concurrency, deploy, invocation, or private prompt/CLI state, and immediately persists one immutable
result before claiming another. Query one batched stable telemetry snapshot for a baseline or two
target batches for an issue and its paired `v0`; never stabilize attempts independently.

Each new verification package binds the reused receipt digest and current verifier commit/digest.
Keep `PASS`, `FAIL`, and `INCOMPLETE` separate under baseline `5/5`, deterministic `5/5` plus paired
`v0` `0/5`, and model-mediated `>=5/7` plus paired `v0` `0/7`. Later failures never discard completed
authority results. A new generation selects only missing, `INCOMPLETE`, or exact-binding-changed
authorities and invokes only those without current exact-bound completed receipts. Composition covers
exactly all 41 authorities. `run-test-agent-validation` is status/next-action guidance only and never
creates sub-sessions or executes phase work.

Reuse matching stable Agents and their exact server-assigned versions. Content changes create a new
version under the same stable name; no command floats `latest`. Retain sessions, responses, Agents,
versions, Hosted topology, images, telemetry, registries, and evidence. Supersede incomplete local
state without deletion; validation has no cleanup.

Validation remains report-free and requires explicit human review before the immutable approval
record or Daily promotion. Never mutate the preserved West US 2 resources or lifecycle, send Daily
smoke traffic, or write private runtime content to Git.
