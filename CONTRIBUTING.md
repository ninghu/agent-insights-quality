# Contributing

Read [AGENTS.md](AGENTS.md). This is a public repository: use synthetic data and public-safe
configuration, never credentials, private identifiers or raw runtime payloads.

## Changes

Use [maintain-test-agents](.github/skills/maintain-test-agents/SKILL.md) for Agent and
issue maintenance. Distinguish repairs to existing targets from changes to the reviewed
inventory. Adding, removing, renaming or reassigning an identity requires explicit scope
agreement, including rotation and scoring coverage; it is not an incidental cleanup.

1. Review the issue's intended root cause, healthy behavior and evidence before changing code.
2. Keep each Agent version independently deployable and isolated to its declared defect.
3. Write small behavior/contract tests with the change; do not restore legacy digest, prose or
   source-layout assertions.
4. Run the smallest relevant offline tests and Ruff. Use each Agent's pinned requirements for
   explicit local Hosted-framework tests; do not modify global Python environments.
5. Compile Bicep for infrastructure changes. Live qualification is a separate, explicitly scoped
   operation against the intended environment, not part of ordinary CI.

For catalog changes, run `python -m agent_insights_quality generate-docs` to update the readable
catalog views, then `python -m agent_insights_quality validate`. Set `PYTHONPATH` to the active
worktree's `src` and confirm module resolution first. Use a reviewed, committed candidate for
authorized staging; its [single-root evidence policy](docs/QUALITY_BAR.md#scoped-single-root-hygiene)
does not permit extra traffic or manual relabeling to force a pass.

The replacement runner still needs its deployed acceptance campaign before scheduled use.
Do not substitute removed legacy flows or enable an unproven candidate.
Normal source review remains required, but staging-to-Daily promotion and digest approval
ceremonies are not part of the design.

## Tests and artifacts

Default pytest collection is `tests/unit`. Keep it offline and quick using small synthetic inputs,
fake clocks/transports and pure functions. Real Hosted-framework tests live in `tests/hosted`;
their dependency sets are intentionally separate from default CI.

Use a separate environment for each Agent's Hosted suite and install that Agent's
`v0/requirements.txt` alongside the shared test extras. The hosting stacks have incompatible
Responses SDK contracts; the common `hosted-test` extra must not force one Responses SDK
version onto every Agent. Do not combine their requirement files or reuse a global environment
whose transitive dependencies another Agent installation has replaced. Keep the suite's
deployed-dependency and actual request/response-shape guards enabled.

Private runtime evidence and local logs belong under
`$HOME\.aiq-runtime\agent-insights-quality\`. Never publish raw traces, provider receipts,
work-item content or private service diagnostics. Preserve historical reports and retained
Azure resources. A private email trial must not publish repository/ADX results or enable automation.
Its approved private Storage archive and time-limited per-Agent links are managed by Python,
not generated-report PRs. Source/catalog/scoring changes still use normal review.
