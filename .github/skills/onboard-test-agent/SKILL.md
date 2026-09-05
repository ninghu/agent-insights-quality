---
name: onboard-test-agent
description: Prepare a reviewed synthetic Agent change and its independently deployable issue versions.
---

# Reviewed Agent changes

Read `AGENTS.md`. Five fixed Agents are a benchmark contract, not an extensible runtime plugin
list. A new Agent requires explicit scope review of inventory, Daily rotation, environment cost,
schemas and score coverage. Do not change existing identities as an incidental refactor.

Define healthy behavior and natural endpoint/trace observability before adding defects.
Each issue owns a complete deployable source tree with one causal defect; shared baseline
build inputs must not introduce defect selectors or patch another version at build time.
Prompt Agents remain tool-free.

Create canonical reviewed attempts and semantic expectations, then exercise actual Hosted
business/framework code locally with controlled external services and in-memory tracing.
Keep these explicit Hosted suites outside default offline CI; no Docker build matrix.
Actual Prompt/hosting/export behavior requires deployed staging evidence.

Update both catalogs and related schemas only with review, regenerate catalog views, and
run targeted checks. Staging is incremental on a committed candidate; Python owns the run.
Preserve the Sweden environments, read-only Application Insights and private runtime state.
Do not add approval/digest workflows or enable Daily automation as part of onboarding.
