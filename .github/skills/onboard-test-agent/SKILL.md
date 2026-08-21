---
name: onboard-test-agent
description: Scaffold a reviewed synthetic test-agent contract safely.
---

# Onboard a Test Agent

1. Read `schemas/agent-manifest.schema.json`, current manifests, scenario compatibility, and
   `docs/OPERATIONS.md`.
2. Choose the next reviewed stable `aiq-NNN-slug` ID. The deployed agent name must use that exact ID
   as its prefix. Reject duplicate IDs or prefixes.
3. Add an immutable healthy implementation contract, synthetic data source, deterministic endpoint
   traffic driver, stable tool definitions, expected task outcomes, and compatibility tags.
4. Set endpoint policy to deployed-endpoint invocation, read-only Application Insights, and forbidden
   direct trace injection. Never create a telemetry shortcut.
5. Do not commit private endpoints, Azure/ADO identifiers, credentials, raw traces, complete
   production prompts, or real data.
6. Add focused contract, healthy-task, trace-completeness, and endpoint-only tests. A baseline cannot
   become active until it passes task completion, adherence, tool selection/input/output use,
   groundedness, safety, efficiency, and zero-insight qualification.
7. Run `python -m agent_insights_quality generate-docs`, validation, and tests.
8. Submit a normal human-reviewed PR. Onboarding is never a generated daily change and never
   auto-activates or deploys the agent.
