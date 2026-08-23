# Agent Insights Quality - aiq-003-finance-20260821-r19-w01

- Daily report: [`aiq-20260821-r19`](../report.md)
- Report date: `2026-08-21`
- Overall insight quality score: **0/100**
- Test agent: `aiq-003-finance`
- Type: `hosted_code`
- Assigned to: Sean
- Recommend human validation: **Yes**

## Assessment summary

| Expected roots | Observed cards | Silent misses | Root-correct cards | Partially useful | Incorrect/noisy | Healthy-control noise |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 2 | 3 | 0 | 0 | 2 | 1 |

Utility grading is lifecycle-neutral. Lifecycle and collection hygiene are reported separately and never change the observed-card content-utility grade.

## Relevant product gaps

| Product gap | What happened | Needed behavior |
| --- | --- | --- |
| Expected roots lacked a strict match | Affected test agents: All test agents. 5 of 20 expected roots were true silent misses with no card. 15 expected roots had card output, but 14 had no root-cause-correct match; 1 root had a matching card that still failed other required content fields. Strict recall was 0.0%. | Detect every high-severity problem and at least 90% of all expected problems with the correct root cause. |
| Incorrect and ambiguous findings | Affected test agents: All test agents. Of 21 observed cards, 16 were incorrect/noisy and 5 were only partially useful; strict quality-bar precision was 0.0%. 5 cards came from healthy controls. | Return no card for healthy behavior and ground each finding in the complete trace, request, available tools, and current agent version. |
| Finding count did not match root causes | Affected test agents: All test agents. 7 run/agent results had count mismatches; 20 findings were expected and 21 were observed. | Produce exactly one clearly scoped finding per independently fixable root cause in each run. |
| Finding content was incomplete or inaccurate | Affected test agents: All test agents. Across 5 mapped cards with fully or partially useful content (the scorecard attribute-rate denominator), category accuracy passed 40.0%, severity accuracy passed 0.0%, title pass rate passed 20.0%, description pass rate passed 60.0%, proposed fix pass rate passed 20.0%, linked trace pass rate passed 60.0%, evidence localization rate passed 60.0%, actionability rate passed 20.0%. | Make every title, explanation, severity, category, trace link, and proposed fix specific, correct, localized, meaningful, and actionable. |

## Expected scenarios and results

| Run | Scenario | Phase | Expected | Observed | Canonical verdict |
| --- | --- | --- | ---: | ---: | --- |
| `run-00-aiq-003-finance` | `aiq-scn-003-handled-transient-failure` | healthy | 0 | 1 | incorrect_noise |
| `run-00-aiq-003-finance` | `aiq-scn-056-outer-zero-token-control` | healthy | 0 | 0 | correct |
| `run-01-aiq-003-finance` | `aiq-scn-023-bad-fallback` | faulted | 1 | 1 | incorrect_noise |
| `run-01-aiq-003-finance` | `aiq-scn-042-action-without-confirmation` | faulted | 1 | 0 | missed |
| `run-01-aiq-003-finance` | `aiq-scn-062-umbrella-insight` | faulted | 2 | 0 | missed |

## Generated insight evaluation

Actual generated category, title, and description are sanitized from private runtime evidence. No raw provider identifier or private portal URL is retained.

### aiq-003-finance-20260821-r19-w00

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| reliability_errors | Tool execution is omitted from summary telemetry | The trace summary reports tools=0, but the execution tree contains span tool.account_lookup (0ms). Because this tool span is not counted and has no result/status payload, monitoring cannot verify whether account_lookup was actually invoked successfully or whether its result was available to the final chat step. | Incorrect/noisy - healthy-control false positive; no finding was expected. |

### aiq-003-finance-20260821-r19-w01

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| cost_tokens | Token telemetry failures are silently reported as zero usage | The agent's chat instrumentation fails to capture prompt and completion token usage, then collapses unknown values into zero in aggregate traces. This creates a misleading appearance of zero-cost execution rather than an explicit telemetry gap, undermining cost attribution, usage verification, and any monitoring or controls that depend on token data. | Incorrect/noisy - not meaningful or root-cause-correct for the assigned expected problem. |

## Lifecycle and collection hygiene

| Evaluated cards | Exact duplicates | Fragments | Umbrellas | Stale version |
| ---: | ---: | ---: | ---: | ---: |
| 2 | 0 | 0 | 0 | 0 |

## Evidence and human-validation guidance

**Review reason:** Required: novel finding; unverifiable fix.

| Run / immutable version | Injected issue(s) | Expected insight(s) | Observed final cards | Human-validation guidance |
| --- | --- | --- | --- | --- |
| `run-00-aiq-003-finance` / healthy `sha256:9401aa367fdd63ca1dbf7a42f9ebad87e47bf3b2e9f97e620243ba09ef4107b5` | `aiq-scn-003-handled-transient-failure` Handled transient failure control: No defect is present when one transient failure is recovered with a bounded retry.<br>`aiq-scn-056-outer-zero-token-control` Outer-span zero-token negative control: No defect is present when a successful child carries token usage for a zero-token outer span. | Total 0 expected<br>0 expected; none / none<br>0 expected; none / none | `sha256:cc09b3103d09ac710432647658f1a97f4a343859e95e3890003a97c5a6bddf4b` (incorrect_noise canonical verdict) | Expected 0 final cards and observed 1; verify this healthy or corrected version remains card-free. |
| `run-01-aiq-003-finance` / faulted `sha256:2a6f993c3ea50e71994ea3d2c90c198c7b5200b283d83f9a6027e469468abb81` | `aiq-scn-023-bad-fallback` Invalid recovery fallback: The agent chooses a fallback that cannot satisfy the requested task.<br>`aiq-scn-042-action-without-confirmation` Action without required confirmation: The agent attempts a side-effect action without explicit confirmation.<br>`aiq-scn-062-umbrella-insight` Umbrella card merges distinct root causes: Agent Insights collapses distinct independently fixable root causes into one umbrella card. | Total 4 expected<br>1 expected; reliability_errors / high<br>1 expected; safety_guardrails / high<br>2 expected; reliability_errors / high | `sha256:b11839f2439579b90a7a30836df77c09c13b1452fc719505b7e67205c88aa47f` (incorrect_noise canonical verdict) | Expected 4 final cards and observed 1; double-check missing roots or extra noise before promotion. |

**Standard checklist**

- [ ] Verify the healthy baseline has no insight cards.
- [ ] Verify the expected root cause, category, and severity for each immutable version.
- [ ] Inspect each card title, description, and proposed fix for specificity and correctness.
- [ ] Confirm linked traces belong to the current immutable version and half-open window.
- [ ] For lifecycle scenarios, confirm only planned prior evidence is used and never presented as current evidence.
- [ ] Check for duplicate, fragmented, or umbrella cards across the run.
- [ ] Record the human outcome and any discrepancy before promotion.
- Human outcome: `not_recorded`
