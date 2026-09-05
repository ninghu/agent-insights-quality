# Contributing

Read [AGENTS.md](AGENTS.md). This is a public repository: use synthetic data and public-safe
configuration, never credentials, private identifiers or raw runtime payloads.

## Changes

1. Review the issue's intended root cause, healthy behavior and evidence before changing code.
2. Keep each Agent version independently deployable and isolated to its declared defect.
3. Write small behavior/contract tests with the change; do not restore legacy digest, prose or
   source-layout assertions.
4. Run the smallest relevant offline tests and Ruff. Use each Agent's pinned requirements for
   explicit local Hosted-framework tests; do not modify global Python environments.
5. Compile Bicep for infrastructure changes. Live qualification is a separate, explicitly scoped
   operation against the intended environment, not part of ordinary CI.

The replacement runner still needs its deployed acceptance campaign before scheduled use.
Do not substitute removed legacy flows or enable an unproven candidate.
Normal source review remains required, but staging-to-Daily promotion and digest approval
ceremonies are not part of the design.

## Tests and artifacts

Default pytest collection is `tests/unit`. Keep it offline and quick using small synthetic inputs,
fake clocks/transports and pure functions. Real Hosted-framework tests live in `tests/hosted`;
their dependency sets are intentionally separate from default CI.

Private runtime evidence and local logs belong under
`$HOME\.aiq-runtime\agent-insights-quality\`. Never publish raw traces, provider receipts,
work-item content or private service diagnostics. Preserve historical reports and retained
Azure resources. A private email trial must not publish repository/ADX results or enable automation.
