# Agent Insights Quality - aiq-002-healthcare-20260821-r19-w02

- Daily report: [`aiq-20260821-r19`](../report.md)
- Report date: `2026-08-21`
- Overall insight quality score: **0/100**
- Test agent: `aiq-002-healthcare`
- Type: `prompt`
- Assigned to: Ilya
- Recommend human validation: **Yes**

## Assessment summary

| Expected roots | Observed cards | Silent misses | Root-correct cards | Partially useful | Incorrect/noisy | Healthy-control noise |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 4 | 1 | 1 | 1 | 3 | 1 |

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
| `run-00-aiq-002-healthcare` | `aiq-scn-057-handled-child-failure-control` | healthy | 0 | 1 | incorrect_noise |
| `run-01-aiq-002-healthcare` | `aiq-scn-009-ignored-user-correction` | faulted | 1 | 1 | incorrect_noise |
| `run-01-aiq-002-healthcare` | `aiq-scn-027-missing-owner-validation` | faulted | 1 | 0 | missed |
| `run-01-aiq-002-healthcare` | `aiq-scn-045-sequential-redundant-calls` | faulted | 1 | 1 | incorrect_noise |
| `run-02-aiq-002-healthcare` | `aiq-scn-028-task-evasion-no-op` | faulted | 1 | 1 | partially_useful |

## Generated insight evaluation

Actual generated category, title, and description are sanitized from private runtime evidence. No raw provider identifier or private portal URL is retained.

### aiq-002-healthcare-20260821-r19-w00

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| reliability_errors | Successful status lacks any recorded agent execution | The trace is marked `status=ok` but records `tokens=0`, `tools=0`, and no assistant final response or `provider_lookup` invocation. For a request explicitly requiring `provider_lookup`, the trace provides no evidence that the required tool was called or that a grounded result was returned. This is a completion/telemetry integrity failure: success was emitted despite the absence of an observable execution outcome. | Incorrect/noisy - healthy-control false positive; no finding was expected. |

### aiq-002-healthcare-20260821-r19-w01

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| reliability_errors | Success status is decoupled from verifiable task completion | The agent/runtime can emit a successful terminal status without any recorded model output, required tool invocation, tool result, or final response. This makes success semantically unreliable for tool-driven workflows: neither requested actions nor their outcomes can be verified, masking silent non-execution and preventing downstream systems from distinguishing completed work from an incomplete or uninstrumented run. | Incorrect/noisy - not meaningful or root-cause-correct for the assigned expected problem. Collection issue: umbrella. |
| tool_call_failures | Tool call was emitted as the final answer without execution | The agent returned a serialized `slot_search` tool-call object as its final assistant output, but the trace contains no corresponding tool-result event and no grounded textual answer describing an available slot. This leaves the requested `slot_search(provider_id="provider-101", date="2030-06-03")` action incomplete despite the trace reporting status=ok. | Incorrect/noisy - not meaningful or root-cause-correct for the assigned expected problem. |

### aiq-002-healthcare-20260821-r19-w02

| Category | Title | Description | Evaluation result |
| --- | --- | --- | --- |
| tool_call_failures | Agent refused a valid direct appointment-creation request | The request supplied every required `appointment_create` field (`patient_id=patient-syn-001`, `provider_id=provider-101`, `date=2030-06-03`, `starts_at=09:00`, `slot_id=slot-101-0900`, and `confirmed=true`) and explicitly prohibited prerequisite lookups. Instead of invoking `appointment_create` with those values, the agent returned an unsupported refusal directing the user to another workflow. This leaves the requested synthetic appointment uncreated despite a schema-valid, fully specified tool call. | Partially useful - expected root cause matched, but category, severity failed. |

## Lifecycle and collection hygiene

| Evaluated cards | Exact duplicates | Fragments | Umbrellas | Stale version |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 0 | 0 | 1 | 0 |

## Evidence and human-validation guidance

**Review reason:** Required: novel finding; partially useful judgment; unverifiable fix.

| Run / immutable version | Injected issue(s) | Expected insight(s) | Observed final cards | Human-validation guidance |
| --- | --- | --- | --- | --- |
| `run-00-aiq-002-healthcare` / healthy `sha256:d457eddb91f0755ef203884e2811aeb6fc35ca3fcca974950a200c5032076240` | `aiq-scn-057-handled-child-failure-control` Handled child failure negative control: No defect is present when the parent handles the child failure and completes correctly. | Total 0 expected<br>0 expected; none / none | `sha256:3051b53183f1dce5bea3e10986641e30b70876c65808774cfa0bfc9e188b9caf` (incorrect_noise canonical verdict) | Expected 0 final cards and observed 1; verify this healthy or corrected version remains card-free. |
| `run-01-aiq-002-healthcare` / faulted `sha256:a347572b64886d9dae4933201c7e777cb1a45a44578c9228a1d198b1be45acf2` | `aiq-scn-009-ignored-user-correction` Ignored explicit user correction: The agent ignores the latest explicit user correction and continues with stale context.<br>`aiq-scn-027-missing-owner-validation` Missing owner or validation in plan: The plan omits required ownership or completion validation.<br>`aiq-scn-045-sequential-redundant-calls` Sequential redundant tool calls: The agent serializes independent calls and creates avoidable end-to-end latency. | Total 3 expected<br>1 expected; context_memory / medium<br>1 expected; output_quality / low<br>1 expected; latency / medium | `sha256:7643e72c3b5d322dbe98904e75d7c4ff73cff94068dc5913e2ad7c482fc3f94a` (incorrect_noise canonical verdict)<br>`sha256:54e4a52abea967288f0454d445ddf91ed7713343cbd5a814971ffdf99f50e647` (incorrect_noise canonical verdict) | Expected 3 final cards and observed 2; double-check missing roots or extra noise before promotion. |
| `run-02-aiq-002-healthcare` / faulted `sha256:ae72c0538babeae4b3efaee595e659801887799a2c185bfe0f246332717d2b6b` | `aiq-scn-028-task-evasion-no-op` Task evasion or no-op response: The agent evades a supported task and performs no useful operation. | Total 1 expected<br>1 expected; reliability_errors / medium | `sha256:42e5acc24932ef7e24fadc218468febeaf1e0032a32c5997809f24648eb86219` (partially_useful canonical verdict) | Expected and observed 1 final cards; double-check each root, category, severity, field, trace link, and collection relationship. |

**Standard checklist**

- [ ] Verify the healthy baseline has no insight cards.
- [ ] Verify the expected root cause, category, and severity for each immutable version.
- [ ] Inspect each card title, description, and proposed fix for specificity and correctness.
- [ ] Confirm linked traces belong to the current immutable version and half-open window.
- [ ] For lifecycle scenarios, confirm only planned prior evidence is used and never presented as current evidence.
- [ ] Check for duplicate, fragmented, or umbrella cards across the run.
- [ ] Record the human outcome and any discrepancy before promotion.
- Human outcome: `not_recorded`
