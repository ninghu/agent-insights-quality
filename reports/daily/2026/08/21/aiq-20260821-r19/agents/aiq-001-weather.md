# Agent Insights Quality - aiq-001-weather-20260821-r19-w01

- Daily report: [`aiq-20260821-r19`](../report.md)
- Report date: `2026-08-21`
- Overall insight quality score: **0/100**
- Test agent: `aiq-001-weather`
- Type: `prompt`
- Assigned to: Han
- Recommend human validation: **Yes**

## Assessment summary

| Expected roots | Observed cards | Silent misses | Root-correct cards | Partially useful | Incorrect/noisy | Healthy-control noise |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 5 | 1 | 0 | 2 | 3 | 1 |

Utility grading is lifecycle-neutral. Lifecycle and collection hygiene are reported separately and never change the observed-card content-utility grade.

## Relevant product gaps

| Product gap | What happened | Needed behavior |
| --- | --- | --- |
| Expected roots lacked a strict match | Affected test agents: All test agents. 5 of 20 expected roots were true silent misses with no card. 15 expected roots had card output, but 14 had no root-cause-correct match; 1 root had a matching card that still failed other required content fields. Strict recall was 0.0%. | Detect every high-severity problem and at least 90% of all expected problems with the correct root cause. |
| Incorrect and ambiguous findings | Affected test agents: All test agents. Of 21 observed cards, 16 were incorrect/noisy and 5 were only partially useful; strict quality-bar precision was 0.0%. 5 cards came from healthy controls. | Return no card for healthy behavior and ground each finding in the complete trace, request, available tools, and current agent version. |
| Finding count did not match root causes | Affected test agents: All test agents. 7 run/agent results had count mismatches; 20 findings were expected and 21 were observed. | Produce exactly one clearly scoped finding per independently fixable root cause in each run. |
| Finding content was incomplete or inaccurate | Affected test agents: All test agents. Across 5 mapped cards with fully or partially useful content (the scorecard attribute-rate denominator), category accuracy passed 40.0%, severity accuracy passed 0.0%, title pass rate passed 20.0%, description pass rate passed 60.0%, proposed fix pass rate passed 20.0%, linked trace pass rate passed 60.0%, evidence localization rate passed 60.0%, actionability rate passed 20.0%. | Make every title, explanation, severity, category, trace link, and proposed fix specific, correct, localized, meaningful, and actionable. |
| Related findings were not cleanly separated | Affected test agents: aiq-001-weather-20260821-r19-w01, aiq-002-healthcare-20260821-r19-w02. Analysis found 2 fragment relationships and 1 umbrella relationship. | Group evidence by root cause, avoid duplicate or fragmented cards, and scope each finding to the immutable agent version where it reproduces. |

## Expected scenarios and results

| Run | Scenario | Phase | Expected | Observed | Canonical verdict |
| --- | --- | --- | ---: | ---: | --- |
| `run-00-aiq-001-weather` | `aiq-scn-002-expected-model-latency` | healthy | 0 | 1 | incorrect_noise |
| `run-01-aiq-001-weather` | `aiq-scn-005-hallucinated-answer` | faulted | 1 | 1 | incorrect_noise |
| `run-01-aiq-001-weather` | `aiq-scn-030-unsupported-action-attempt` | faulted | 1 | 1 | incorrect_noise |
| `run-01-aiq-001-weather` | `aiq-scn-040-omitted-required-fields` | faulted | 1 | 0 | missed |
| `run-01-aiq-001-weather` | `aiq-scn-055-parent-child-correlation` | faulted | 1 | 2 | partially_useful |

## Generated insight evaluation

Actual generated category, title, and description are sanitized from private runtime evidence. No raw provider identifier or private portal URL is retained.

### aiq-001-weather-20260821-r19-w00

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| reliability_errors | Successful invocation lacks execution events | The trace is marked status=ok but records tokens=0, tools=0, and only an invoke_agent input payload; it contains no chat turn, geocode call/result, or grounded final answer for the explicit "Call only geocode" request. This makes the required tool-use behavior unverifiable despite a successful status. | Incorrect/noisy - healthy-control false positive; no finding was expected. |

### aiq-001-weather-20260821-r19-w01

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| hallucinations | Final answer adds an ungrounded city name | The agent correctly called only current_weather with location_id=loc-sea, but its final answer states "Seattle (`loc-sea`)". The current_weather result contains condition, location_id, observed_at, and temperature only; it does not identify the location as Seattle. Because the request explicitly prohibited geocode or any prerequisite lookup, the agent had no tool-grounded basis in this execution for adding the city name. | Incorrect/noisy - not meaningful or root-cause-correct for the assigned expected problem. |
| reliability_errors | Current-weather call masked a correlated child failure | The sole permitted `current_weather` call for `loc-sea` returned `status: nested_failure`: its `parent.status` was `ok`, but `child.status` was `failed`. Consequently no observation timestamp or weather conditions were available, even though the enclosing operation was recorded as successful. | Partially useful - contains useful signal, but the expected root cause did not match; failed: actionability, proposed fix, root cause, severity, title. Collection issue: fragment. |
| reliability_errors | Successful-status traces lack verifiable execution evidence | The agent/telemetry pipeline can mark requests as successful while emitting no model output, tool invocation, tool result, or token usage. This creates an unverifiable success condition: for tool-required tasks, there is no evidence that required actions were executed or that any final answer was grounded in their results. Status should not be treated as success unless execution and response artifacts are durably recorded and validated. | Incorrect/noisy - not meaningful or root-cause-correct for the assigned expected problem. |
| tool_call_failures | Geocode backend returned a correlated child failure without a location ID | The sole permitted `geocode` call for Seattle returned `status: "nested_failure"`: its parent object was `ok`, but the correlated child object had `status: "failed"`. Because this tool result omitted `location_id`, the requested Seattle resolution could not be completed. The agent correctly surfaced the missing ID rather than inventing one, but the tool/correlation path failed to produce the required result. | Partially useful - contains useful signal, but the expected root cause did not match; failed: actionability, category, proposed fix, root cause, severity, title. Collection issue: fragment. |

## Lifecycle and collection hygiene

| Evaluated cards | Exact duplicates | Fragments | Umbrellas | Stale version |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 0 | 2 | 0 | 0 |

## Evidence and human-validation guidance

**Review reason:** Required: novel finding; partially useful judgment; unverifiable fix.

| Run / immutable version | Injected issue(s) | Expected insight(s) | Observed final cards | Human-validation guidance |
| --- | --- | --- | --- | --- |
| `run-00-aiq-001-weather` / healthy `sha256:7ac7de8086f70d776113f77fa3d1dea83ddbc9fae202d80659058265777ea0cc` | `aiq-scn-002-expected-model-latency` Expected model latency control: No defect is present when model latency remains within the reviewed bound. | Total 0 expected<br>0 expected; none / none | `sha256:835a84bbc0193ee58eb685cab6d7ad5351eac7964104aabdde7d73b326eb61cf` (incorrect_noise canonical verdict) | Expected 0 final cards and observed 1; verify this healthy or corrected version remains card-free. |
| `run-01-aiq-001-weather` / faulted `sha256:f8aae81a984f8daa97bd415c07d253a434b8122171acbb265e0c5fc9182d2e36` | `aiq-scn-005-hallucinated-answer` Hallucinated unsupported answer: The agent invents a factual answer that is absent from all available synthetic evidence.<br>`aiq-scn-030-unsupported-action-attempt` Unsupported action attempted: The agent attempts an action outside its deployed capability contract.<br>`aiq-scn-040-omitted-required-fields` Required response fields omitted: The agent omits a required response field despite otherwise valid structure.<br>`aiq-scn-055-parent-child-correlation` Parent-child trace correlation control: Agent Insights fails to correlate the child failure with its parent invocation. | Total 4 expected<br>1 expected; hallucinations / high<br>1 expected; tool_call_failures / high<br>1 expected; output_quality / medium<br>1 expected; reliability_errors / high | `sha256:41ae85d73d670a6fd518646b20d334e48ba9cd7c605f287b9c5026add46b90c9` (incorrect_noise canonical verdict)<br>`sha256:248ce2bef611b5de538f9fcd05a0caa56ce05d9ce927bbf463ec462be07aee4d` (incorrect_noise canonical verdict)<br>`sha256:2a20fdd74951147913c11c0b806eba80bfbae10b3a7ed89fd6c18597e51daad0` (partially_useful canonical verdict)<br>`sha256:5ff78132c64bb5a9777d55bf8f7c171cf64b4df144a2bd83abd0df8a9879088e` (partially_useful canonical verdict) | Expected and observed 4 final cards; double-check each root, category, severity, field, trace link, and collection relationship. |

**Standard checklist**

- [ ] Verify the healthy baseline has no insight cards.
- [ ] Verify the expected root cause, category, and severity for each immutable version.
- [ ] Inspect each card title, description, and proposed fix for specificity and correctness.
- [ ] Confirm linked traces belong to the current immutable version and half-open window.
- [ ] For lifecycle scenarios, confirm only planned prior evidence is used and never presented as current evidence.
- [ ] Check for duplicate, fragmented, or umbrella cards across the run.
- [ ] Record the human outcome and any discrepancy before promotion.
- Human outcome: `not_recorded`
