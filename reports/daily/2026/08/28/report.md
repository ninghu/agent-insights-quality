# Agent Insights Quality - 2026-08-28

## Summary

| Grade | Findings |
| --- | --- |
| **FAIL** | Score **35.5/100** (-8.6 vs 2026-08-26) (PASS threshold 90/100); 3 matched, 4 partial, 10 noise cards |
| Expected issue Insights | 25 |
| Expected baseline Insights | 0 |
| Observed cards | 27 |

`field_weighted_v1`: field quality `30.6/100` at 85%; clean-card precision `63/100` at 15%.

## What is working

| Capability | Evidence |
| --- | --- |
| Baseline health | 1 of 5 Agents produced zero baseline Insights |
| Exact issue quality | 3 of 25 selected issues passed every field |
| Noise | 10 false-positive, unrelated, or duplicate cards |

## Baseline ownership

| Agent | Cards | Verdict | Ownership |
| --- | ---: | --- | --- |
| `weather-agent` | 1 | `agent_finding` | `agent` |
| `healthcare-agent` | 1 | `agent_finding` | `agent` |
| `finance-agent` | 1 | `agent_finding` | `agent` |
| `travel-agent` | 0 | `clean` | `none` |
| `support-ticket-agent` | 1 | `noise` | `insight_engine` |

## What needs improvement

| Issue | Agent | Result | Ownership |
| --- | --- | --- | --- |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-004` - Standing units preference forgotten | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-002` - Explicit response schema violated | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-001` - Unsupported factual answer | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-009` - Provider correction ignored | `healthcare-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | `FAIL` / Noise | `insight_engine` |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-011` - Appointment created without confirmation | `healthcare-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-015` - Finance tool uses wrong account scope | `finance-agent` | `FAIL` / Duplicate | `insight_engine` |
| `issue-017` - Partial finance result reported complete | `finance-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-014` - Required account identifier omitted | `finance-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-019` - Permanent finance failure retried in a loop | `finance-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-026` - One itinerary dropped from comparison | `travel-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-024` - Travel inventory payload overfetched | `travel-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-028` - Stale itinerary state reused | `travel-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-033` - Ticket result omitted after successful tool | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-029` - Required ticket escalation omitted | `support-ticket-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-031` - Ticket orchestration makes no progress | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |

## Human validation

| Issue | Agent | Cards | Sol verdict | Ownership | Confidence |
| --- | --- | ---: | --- | --- | ---: |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | 2 | Partially Correct | `insight_engine` | 0.96 |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | 1 | Partially Correct | `insight_engine` | 0.96 |
| `issue-004` - Standing units preference forgotten | `weather-agent` | 0 | Missing | `insight_engine` | 0.98 |
| `issue-002` - Explicit response schema violated | `weather-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-001` - Unsupported factual answer | `weather-agent` | 0 | Missing | `insight_engine` | 0.98 |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-009` - Provider correction ignored | `healthcare-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | 2 | Noise | `insight_engine` | 0.99 |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | 1 | Incorrect | `insight_engine` | 0.98 |
| `issue-011` - Appointment created without confirmation | `healthcare-agent` | 2 | Missing | `insight_engine` | 0.99 |
| `issue-015` - Finance tool uses wrong account scope | `finance-agent` | 2 | Duplicate | `insight_engine` | 0.99 |
| `issue-017` - Partial finance result reported complete | `finance-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-014` - Required account identifier omitted | `finance-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-019` - Permanent finance failure retried in a loop | `finance-agent` | 2 | Partially Correct | `insight_engine` | 0.99 |
| `issue-018` - Required transient retry omitted | `finance-agent` | 1 | Correct | `none` | 0.99 |
| `issue-023` - Required inventory search omitted | `travel-agent` | 1 | Correct | `none` | 0.99 |
| `issue-021` - Inventory fabricated after search failure | `travel-agent` | 1 | Correct | `none` | 0.99 |
| `issue-026` - One itinerary dropped from comparison | `travel-agent` | 1 | Missing | `insight_engine` | 0.99 |
| `issue-024` - Travel inventory payload overfetched | `travel-agent` | 1 | Missing | `insight_engine` | 0.99 |
| `issue-028` - Stale itinerary state reused | `travel-agent` | 2 | Partially Correct | `insight_engine` | 0.99 |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-033` - Ticket result omitted after successful tool | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-029` - Required ticket escalation omitted | `support-ticket-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-031` - Ticket orchestration makes no progress | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |

## Per-Agent reports

- [finance-agent](agents/finance-agent.md)
- [healthcare-agent](agents/healthcare-agent.md)
- [support-ticket-agent](agents/support-ticket-agent.md)
- [travel-agent](agents/travel-agent.md)
- [weather-agent](agents/weather-agent.md)
