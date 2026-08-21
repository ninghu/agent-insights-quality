---
name: onboard-test-scenario
description: Scaffold a reviewed deterministic scenario and ground-truth contract.
---

# Onboard a Test Scenario

1. Read `schemas/scenario-manifest.schema.json`, the catalog, agent compatibility, and quality rubric.
2. Choose the next reviewed stable `aiq-scn-NNN-slug` ID and semantic version. Reject duplicates.
3. Define priority/customer impact, compatible domains/types/agents, conflict tags, exact immutable
   healthy-to-fault mutation, endpoint traffic recipe, deterministic seed namespace, expected
   spans/tool calls/evidence count, negative controls, root cause, public category, severity, fix
   boundary, and cross-version behavior.
4. Keep the healthy baseline immutable; materialize mutations only in temporary build directories.
5. Traffic must invoke a deployed endpoint and use generated synthetic data. Never inject telemetry
   or commit private identifiers, raw traces, credentials, private ADO content, or real data.
6. Add focused schema, mutation, traffic, compatibility/conflict, evidence, and expected-outcome
   tests. Include healthy decoys and avoid ambiguous co-location.
7. Run documentation generation, repository validation, and tests.
8. Submit a normal human-reviewed PR. A discovered product gap is only a candidate until this review
   establishes ground truth; daily automation cannot invent, activate, retire, or weaken a scenario.
9. Treat the catalog as the predefined issue library, not the daily assignment. P0 faults and healthy
   controls are mandatory daily; P1/P2 faults enter the deterministic six-day rotation. Confirm that
   the policy can still partition and assign the complete rotating library within four expected roots
   per agent, or fail the onboarding change for human redesign.
