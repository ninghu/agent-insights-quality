---
name: staging-qualification
description: Run the durable Sweden Central staging qualification for a reviewed candidate.
license: MIT
---

# Sweden Central Staging Qualification

Use `.github/skills/test-agent-validation/SKILL.md` as the executable contract.

Prepare the one durable Sweden Central `g30` `aiq-staging-swedencentral` Project and all 41 unique catalog authorities before traffic.
Never create or delete the Project during validation.
The coordinator publishes immutable disjoint deployment assignments, releases its global lock,
and runs up to eight asynchronous deployment workers. Only the coordinator may reconcile all exact
versions and atomically publish shared topology and registry state. Validation invocation runs in
waves of at most eight, followed by deferred read-only trace verification in waves of at most four.
Composition accepts only exact matching receipts and reusable PASS evidence covering all 41 authorities.

Reuse matching stable Agents and their exact server-assigned versions. Content changes create a new
version under the same stable name; no command floats `latest`. Retain sessions, responses, Agents,
versions, Hosted topology, images, telemetry, registries, and evidence. Supersede incomplete local
state without deletion.

Validation remains report-free and requires explicit human review before the immutable approval
record or Daily promotion. Never mutate the preserved West US 2 resources or lifecycle, send Daily
smoke traffic, or write private runtime content to Git.
