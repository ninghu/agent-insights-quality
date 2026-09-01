---
name: staging-qualification
description: Run the durable Sweden Central staging qualification for a reviewed candidate.
license: MIT
---

# Sweden Central Staging Qualification

Use `.github/skills/test-agent-validation/SKILL.md` as the executable contract.

Prepare the one durable Sweden Central `g30` `aiq-staging-swedencentral` Project and all 41 stable catalog authorities before traffic.
Never create or delete the Project during validation.
The coordinator then assigns exactly 10 explicit shards, runs invocation in waves of at most eight,
waits at the barrier, and runs deferred read-only trace verification in waves of at most four.
Composition accepts only exact matching packages covering all 41 authorities.

Reuse matching stable Agents and their exact server-assigned versions. Content changes create a new
version under the same stable name; no command floats `latest`. Cleanup removes only run-scoped
sessions, responses, and temporary artifacts while retaining the Project and all durable Agent and
Hosted topology.

Validation remains report-free and requires explicit human review before the immutable approval
record or Daily promotion. Never mutate the preserved West US 2 resources or lifecycle, send Daily
smoke traffic, or write private runtime content to Git.
