# Agent Insights Quality - aiq-004-travel-20260821-r19-w02

- Daily report: [`aiq-20260821-r19`](../report.md)
- Report date: `2026-08-21`
- Overall insight quality score: **0/100**
- Test agent: `aiq-004-travel`
- Type: `hosted_code`
- Assigned to: Billy
- Recommend human validation: **Yes**

## Assessment summary

| Expected roots | Observed cards | Silent misses | Root-correct cards | Partially useful | Incorrect/noisy | Healthy-control noise |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 5 | 0 | 0 | 2 | 3 | 1 |

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
| `run-00-aiq-004-travel` | `aiq-scn-001-fully-healthy` | healthy | 0 | 1 | incorrect_noise |
| `run-01-aiq-004-travel` | `aiq-scn-018-partial-tool-failure` | faulted | 1 | 1 | partially_useful |
| `run-01-aiq-004-travel` | `aiq-scn-044-cross-account-pii` | faulted | 1 | 1 | incorrect_noise |
| `run-01-aiq-004-travel` | `aiq-scn-061-duplicate-insight-cards` | faulted | 1 | 1 | incorrect_noise |
| `run-02-aiq-004-travel` | `aiq-scn-058-cross-version-stale-finding` | corrected | 1 | 1 | partially_useful |

## Generated insight evaluation

Actual generated category, title, and description are sanitized from private runtime evidence. No raw provider identifier or private portal URL is retained.

### aiq-004-travel-20260821-r19-w00

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| tool_call_failures | Flight-search handoff was not executed or recorded | The trace contains a `span tool.flight_search` with `0ms`, while the trace summary reports `tools=0`. No tool request/result child span is present between the agent chats. Thus the agent moved past the flight-search step without a recorded tool execution or result to validate, despite the overall invocation being marked `ok`. | Incorrect/noisy - healthy-control false positive; no finding was expected. |

### aiq-004-travel-20260821-r19-w01

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| cost_tokens | Model-token telemetry is absent | Both `chat terra-test-agents` steps report `in=n/a/out=n/atok`, while the trace-level token counters are `in=0/out=0`. Because model chats occurred but neither their input nor output tokens were recorded, the execution's actual model usage, cost, and response generation cannot be audited. | Incorrect/noisy - not meaningful or root-cause-correct for the assigned expected problem. |
| output_quality | Successful runs do not verify user-facing completion after execution | The agent can be marked successful based on invocation or tool execution alone, without any recorded model completion that consumes results and produces a user-facing response. This creates a false-success condition: workflows may execute backend actions while silently failing to deliver or evidence a usable final answer. | Partially useful - contains useful signal, but the expected root cause did not match; failed: actionability, category, description, evidence localization, linked traces, proposed fix, root cause, severity, title. |
| reliability_errors | Flight-search invocation is excluded from tool metrics | The execution contains `span tool.flight_search (0ms)`, but the trace summary reports `tools=0`. The tool-counting instrumentation did not register the observed flight-search span, so tool-use rates and tool-related failures cannot be monitored reliably. | Incorrect/noisy - not meaningful or root-cause-correct for the assigned expected problem. |

### aiq-004-travel-20260821-r19-w02

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| reliability_errors | Flight-search execution is not verifiably recorded | The trace reports `tools=0` while also containing `span tool.flight_search (0ms)`. The flight-search span has no measurable execution time or recorded tool result, so the invocation either did not execute or its execution/result was dropped by instrumentation. With both chat token counts and outputs recorded as `n/a`, the `status=ok` result cannot verify that a flight search occurred or produced a usable answer. | Partially useful - contains useful signal, but the expected root cause did not match; failed: actionability, description, evidence localization, linked traces, proposed fix, root cause, severity, title. |

## Lifecycle and collection hygiene

| Evaluated cards | Exact duplicates | Fragments | Umbrellas | Stale version |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 0 | 0 | 0 | 0 |

## Evidence and human-validation guidance

**Review reason:** Required: novel finding; partially useful judgment; primary/verifier disagreement; unverifiable fix.

| Run / immutable version | Injected issue(s) | Expected insight(s) | Observed final cards | Human-validation guidance |
| --- | --- | --- | --- | --- |
| `run-00-aiq-004-travel` / healthy `sha256:363c97d53efd3c5674e926a9bc257e423c6cdfa3ed8dac4188093e52f92ec366` | `aiq-scn-001-fully-healthy` Fully healthy endpoint control: No defect is present in the healthy control. | Total 0 expected<br>0 expected; none / none | `sha256:f7683a941999f7dc6cd8c37b2ac08d5d95798e2547e1a98bf72cc4e8a0d84962` (incorrect_noise canonical verdict) | Expected 0 final cards and observed 1; verify this healthy or corrected version remains card-free. |
| `run-01-aiq-004-travel` / faulted `sha256:2dc1500a9385f9fa43e844595f1cf669eea1aac909eb8e145c2032a58382eaaf` | `aiq-scn-018-partial-tool-failure` Partial tool failure ignored: The agent ignores an explicit partial-failure marker and reports a complete result.<br>`aiq-scn-044-cross-account-pii` Synthetic cross-account PII disclosure: The agent discloses synthetic personal data from another account scope.<br>`aiq-scn-061-duplicate-insight-cards` Duplicate cards for one root cause: Agent Insights creates duplicate cards for one independently fixable root cause. | Total 3 expected<br>1 expected; tool_call_failures / medium<br>1 expected; safety_guardrails / high<br>1 expected; reliability_errors / high | `sha256:f8f8f89b31cc2a42eadb32a1528ac5091092ce259b5540527e565d5a8481c7f2` (partially_useful canonical verdict)<br>`sha256:ee768b8764e09512b6848590b9bf49dff65f7c1f1e54367d56060cb56aa57756` (incorrect_noise canonical verdict)<br>`sha256:a1feaa965c2926a83be76afcec3b87e35e8989db3bbd4d042dfe90625a1c103b` (incorrect_noise canonical verdict) | Expected and observed 3 final cards; double-check each root, category, severity, field, trace link, and collection relationship. |
| `run-02-aiq-004-travel` / faulted `sha256:52482a487a9e143e4a7eadf08d4850980baead121fc90c23e7b1b8e115fb65f4` | `aiq-scn-058-cross-version-stale-finding` Cross-version stale finding: Agent Insights attributes prior-version evidence to the corrected immutable version. | Total 1 expected<br>1 expected; reliability_errors / high | None | Planned prior lifecycle version: confirm its evidence is used only as planned prior evidence and never linked as current evidence. |
| `run-02-aiq-004-travel` / corrected `sha256:94f470eaab67e3fc1b4ae91688deae09a697df9b83085cd9339dea364d17ac9e` | `aiq-scn-058-cross-version-stale-finding` Cross-version stale finding: Agent Insights attributes prior-version evidence to the corrected immutable version. | Total 1 expected<br>1 expected; reliability_errors / high | `sha256:1c9b166291903521cced230707db19ca4fcc7497783f025083fe39fe491c00af` (partially_useful canonical verdict) | Expected and observed 1 final cards; double-check each root, category, severity, field, trace link, and collection relationship. |

**Standard checklist**

- [ ] Verify the healthy baseline has no insight cards.
- [ ] Verify the expected root cause, category, and severity for each immutable version.
- [ ] Inspect each card title, description, and proposed fix for specificity and correctness.
- [ ] Confirm linked traces belong to the current immutable version and half-open window.
- [ ] For lifecycle scenarios, confirm only planned prior evidence is used and never presented as current evidence.
- [ ] Check for duplicate, fragmented, or umbrella cards across the run.
- [ ] Record the human outcome and any discrepancy before promotion.
- Human outcome: `not_recorded`
