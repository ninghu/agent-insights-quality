# Contributing

Read [AGENTS.md](AGENTS.md). This is a public repository: use synthetic data and public-safe
configuration, never credentials, private identifiers or raw runtime payloads.

## Changes

Use [maintain-test-agents](.github/skills/maintain-test-agents/SKILL.md) for Agent and
issue maintenance. Distinguish repairs to existing targets from changes to the reviewed
inventory. Adding, removing, renaming or reassigning an identity requires explicit scope
agreement, including rotation and scoring coverage; it is not an incidental cleanup.

The same skill supports an explicitly authorized
[bounded maintenance repair loop](docs/OPERATIONS.md#bounded-maintenance-repair-loop)
for unintended Agent or related framework defects. It reuses the Python staging/Daily
runner and private report handoff; it does not authorize service changes, alter official
automation or repair intentionally injected defects. Scope and native/PR actions stay bounded.
New Agent/issue onboarding TEST reports use the initiating contributor's explicitly supplied
private mailbox, frozen per run, rather than a fixed maintainer destination.

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

## Support handoff and inventory history

The explicitly approved issue-007 reassignment preserves five Agents, 36 issues and 41 targets:
Healthcare owns issues 008–012 and Support owns 007 plus 029–036. Other identities are unchanged.
Use catalog membership, not numeric ranges, for execution and test-version coverage. Daily still
selects a baseline plus four distinct issues per Agent (25 units, 20 issues); Support's nine issues
require three consecutive weekday plans for full rotation coverage rather than two. Private weekend
planning remains date-bound, not a rewritten weekday run.

Every Support source version accepts a read-only ticket handoff with caller-supplied `owner`,
`next_action`, `deadline` and `validation`. `TicketSession` reads the ticket and collects a typed
`prepare_handoff` result through `ObservedSession`, preserving tool arguments/results and actual
root input/output spans. Only issue-007's final JSON field projection drops `deadline` and
`validation`; other Support versions serialize all four fields. No contrary model instruction,
discarded model work, ticket mutation or synthetic model payload is involved. Ordinary Support
model, authorization, revision, retry and polling paths retain their existing contracts.

The canonical `traffic.json` has ten ordered setup/probe attempts, two synthetic tickets and ten
different owners/deadlines. Each probe visibly requires all four fields and supplies their values.
Full upstream facts versus the incomplete delivered JSON establish the defect; labels do not.
Its deterministic mode describes this unconditional code projection, not a qualification outcome.
Assess all ten under the unchanged eight-observation/no-resampling policy. Local matched Hosted
tests are not deployed acceptance.

Baseline attempt 6 replaces a duplicate of attempt 1's read with the healthy handoff. It retains
ten attempts and every prior coverage family, including ordinary model work and handled failures.
Traffic for issues 029–036 is unchanged; their complete source copies gain only the healthy handoff.

The new Support target needs fresh deployment and evidence. Changed Support source selects all ten
Support targets for traffic. Catalog inventory edits currently conservatively select other completed
targets for reassessment of retained usable evidence, not redeployment or automatic fresh traffic;
missing/incomplete work is evaluated against its actual records. Preserve old Healthcare bindings,
resources and failed results; a changed owner never transfers old evidence to the new target.
Exact historical email exports and prepared private publications restore their frozen plans, dates,
policies and bytes, without consulting current rotation. Restyling against incompatible current
ownership may fail with `report_context_identity_mismatch`; do not rewrite the old plan to bypass it.

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
