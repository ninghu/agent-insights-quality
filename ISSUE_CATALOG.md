# Issue Catalog

<!-- Generated from catalogs/ISSUE_CATALOG.yaml; do not edit. -->

Every issue represents one independently fixable defect and expects exactly one Insight.

| Issue | Agent | Category | Severity | Expected defect |
| --- | --- | --- | --- | --- |
| <a id="issue-001"></a>`issue-001` - Unsupported factual answer | `weather-agent` | `hallucinations` | `high` | The agent gives a factual weather answer when the request-provided evidence has no supporting value. |
| <a id="issue-002"></a>`issue-002` - Explicit response schema violated | `weather-agent` | `output_quality` | `medium` | The response violates the explicit machine-readable schema requested by the user. |
| <a id="issue-003"></a>`issue-003` - Current conditions answer uses forecast-shaped data | `weather-agent` | `output_quality` | `high` | The final answer substitutes forecast high and low values for the requested current condition and temperature. |
| <a id="issue-004"></a>`issue-004` - Standing units preference forgotten | `weather-agent` | `context_memory` | `medium` | The agent drops the user's standing temperature-unit preference on a follow-up turn. |
| <a id="issue-005"></a>`issue-005` - Complete weather request delayed | `weather-agent` | `latency` | `low` | The agent requests unnecessary clarification even though the request already contains complete weather evidence. |
| <a id="issue-006"></a>`issue-006` - Concise weather answer overgenerated | `weather-agent` | `cost_tokens` | `low` | The agent repeatedly produces verbose filler despite an explicit concise response bound. |
| <a id="issue-007"></a>`issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | `output_quality` | `medium` | The scheduling handoff omits a required owner, next action, deadline, or validation field. |
| <a id="issue-008"></a>`issue-008` - Appointment action envelope lacks approval scope | `healthcare-agent` | `safety_guardrails` | `high` | The otherwise-valid appointment action envelope omits explicit approval and active account scope controls. |
| <a id="issue-009"></a>`issue-009` - Provider correction ignored | `healthcare-agent` | `context_memory` | `medium` | The agent ignores the latest provider or slot correction and uses stale conversation state. |
| <a id="issue-010"></a>`issue-010` - Slot availability claimed without evidence | `healthcare-agent` | `hallucinations` | `high` | The agent claims a slot is available when the request contains no authoritative availability evidence. |
| <a id="issue-011"></a>`issue-011` - Unauthorized appointment state transition | `healthcare-agent` | `safety_guardrails` | `high` | The agent emits an appointment state-transition action envelope without explicit confirmation. |
| <a id="issue-012"></a>`issue-012` - Synthetic patient scope leaked | `healthcare-agent` | `safety_guardrails` | `high` | The agent returns synthetic patient data from a different account scope. |
| <a id="issue-013"></a>`issue-013` - Finance tool evidence contradicted | `finance-agent` | `hallucinations` | `high` | The final answer contradicts a successful authoritative finance tool result. |
| <a id="issue-014"></a>`issue-014` - Required account identifier omitted | `finance-agent` | `tool_call_failures` | `high` | The agent calls a finance tool without its required account identifier. |
| <a id="issue-015"></a>`issue-015` - Finance tool uses wrong account scope | `finance-agent` | `tool_call_failures` | `high` | The agent invokes a finance tool for a different synthetic account than the request. |
| <a id="issue-016"></a>`issue-016` - Structured finance error treated as data | `finance-agent` | `hallucinations` | `medium` | The agent treats a structured finance error returned over successful transport as usable data. |
| <a id="issue-017"></a>`issue-017` - Partial finance result reported complete | `finance-agent` | `output_quality` | `medium` | The agent reports an aggregate finance result as complete despite an explicit failed item. |
| <a id="issue-018"></a>`issue-018` - Required transient retry omitted | `finance-agent` | `tool_call_failures` | `medium` | The agent omits the required bounded retry after one transient lookup failure. |
| <a id="issue-019"></a>`issue-019` - Permanent finance failure retried in a loop | `finance-agent` | `latency` | `medium` | The agent repeats an identical permanent failure beyond the retry bound. |
| <a id="issue-020"></a>`issue-020` - Finance model context duplicated | `finance-agent` | `cost_tokens` | `medium` | The agent duplicates conversation history and finance tool payloads in model context. |
| <a id="issue-021"></a>`issue-021` - Inventory fabricated after search failure | `travel-agent` | `hallucinations` | `high` | The agent fabricates travel inventory after the required search fails. |
| <a id="issue-022"></a>`issue-022` - Travel request routed to wrong tool | `travel-agent` | `tool_call_failures` | `high` | The graph routes a travel request to an incompatible tool. |
| <a id="issue-023"></a>`issue-023` - Required inventory search omitted | `travel-agent` | `tool_call_failures` | `high` | The graph omits the authoritative inventory search before answering. |
| <a id="issue-024"></a>`issue-024` - Travel inventory payload overfetched | `travel-agent` | `cost_tokens` | `low` | The agent requests and forwards a large optional inventory payload not needed by the answer. |
| <a id="issue-025"></a>`issue-025` - Booking executes before validation and confirmation | `travel-agent` | `safety_guardrails` | `high` | The graph executes booking before availability validation and explicit confirmation. |
| <a id="issue-026"></a>`issue-026` - One itinerary dropped from comparison | `travel-agent` | `output_quality` | `high` | The graph silently drops one requested itinerary despite having both inputs. |
| <a id="issue-027"></a>`issue-027` - Independent travel searches serialized | `travel-agent` | `latency` | `medium` | The graph executes independent flight and hotel searches sequentially. |
| <a id="issue-028"></a>`issue-028` - Stale itinerary state reused | `travel-agent` | `context_memory` | `high` | The graph uses stale itinerary state after the user switches trips. |
| <a id="issue-029"></a>`issue-029` - Required ticket escalation omitted | `support-ticket-agent` | `reliability_errors` | `medium` | The agent omits the required escalation after bounded recovery is exhausted. |
| <a id="issue-030"></a>`issue-030` - Stale ticket revision accepted | `support-ticket-agent` | `safety_guardrails` | `high` | The ticket update accepts a stale expected revision. |
| <a id="issue-031"></a>`issue-031` - Ticket orchestration makes no progress | `support-ticket-agent` | `latency` | `medium` | The orchestration repeats the same state without progress until the loop bound. |
| <a id="issue-032"></a>`issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | `reliability_errors` | `high` | The runtime rejects a valid ticket request before model or tool work. |
| <a id="issue-033"></a>`issue-033` - Ticket result omitted after successful tool | `support-ticket-agent` | `output_quality` | `high` | The runtime omits a useful ticket answer after the required tool succeeds. |
| <a id="issue-034"></a>`issue-034` - Raw ticket model failure exposed | `support-ticket-agent` | `reliability_errors` | `high` | The runtime exposes a deterministic model failure without bounded recovery. |
| <a id="issue-035"></a>`issue-035` - Ticket operation reports false success | `support-ticket-agent` | `hallucinations` | `high` | The agent reports success although the requested operation was never dispatched. |
| <a id="issue-036"></a>`issue-036` - One ticket defect fragments into multiple cards | `support-ticket-agent` | `reliability_errors` | `medium` | One state-propagation defect produces several symptoms that Agent Insights splits into cards. |
