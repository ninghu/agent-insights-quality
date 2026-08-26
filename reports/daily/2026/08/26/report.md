# Agent Insights Quality - 2026-08-26

## Summary

| Grade | Findings |
| --- | --- |
| **FAIL** | Score **44.1/100** (PASS threshold 90/100); 4 matched, 9 partial, 18 noise cards |
| Expected issue Insights | 25 |
| Expected baseline Insights | 0 |
| Observed cards | 32 |

`field_weighted_v1`: field quality `44.2/100` at 85%; clean-card precision `43.8/100` at 15%.

## What is working

| Capability | Evidence |
| --- | --- |
| Baseline health | 1 of 5 Agents produced zero baseline Insights |
| Exact issue quality | 4 of 25 selected issues passed every field |
| Noise | 18 false-positive, unrelated, or duplicate cards |

## Baseline ownership

| Agent | Cards | Verdict | Ownership |
| --- | ---: | --- | --- |
| `weather-agent` | 1 | `noise` | `insight_engine` |
| `healthcare-agent` | 2 | `noise` | `insight_engine` |
| `finance-agent` | 2 | `noise` | `insight_engine` |
| `travel-agent` | 0 | `clean` | `none` |
| `support-ticket-agent` | 2 | `noise` | `insight_engine` |

## What needs improvement

| Issue | Agent | Result | Ownership |
| --- | --- | --- | --- |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-004` - Standing units preference forgotten | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-001` - Unsupported factual answer | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-002` - Explicit response schema violated | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-009` - Provider correction ignored | `healthcare-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-017` - Partial finance result reported complete | `finance-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-016` - Structured finance error treated as data | `finance-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-018` - Required transient retry omitted | `finance-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-025` - Booking executes before validation and confirmation | `travel-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-028` - Stale itinerary state reused | `travel-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-024` - Travel inventory payload overfetched | `travel-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-027` - Independent travel searches serialized | `travel-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-029` - Required ticket escalation omitted | `support-ticket-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-033` - Ticket result omitted after successful tool | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-031` - Ticket orchestration makes no progress | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |

## Human validation

| Issue | Agent | Cards | Sol verdict | Ownership | Confidence |
| --- | --- | ---: | --- | --- | ---: |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | 1 | Missing | `insight_engine` | 0.99 |
| `issue-004` - Standing units preference forgotten | `weather-agent` | 2 | Missing | `insight_engine` | 0.99 |
| `issue-001` - Unsupported factual answer | `weather-agent` | 2 | Missing | `insight_engine` | 0.99 |
| `issue-002` - Explicit response schema violated | `weather-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | 1 | Partially Correct | `insight_engine` | 0.96 |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | 1 | Partially Correct | `insight_engine` | 0.99 |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | 2 | Incorrect | `insight_engine` | 0.98 |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-009` - Provider correction ignored | `healthcare-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | 1 | Partially Correct | `insight_engine` | 0.99 |
| `issue-017` - Partial finance result reported complete | `finance-agent` | 1 | Partially Correct | `insight_engine` | 0.99 |
| `issue-014` - Required account identifier omitted | `finance-agent` | 1 | Correct | `none` | 0.99 |
| `issue-016` - Structured finance error treated as data | `finance-agent` | 1 | Partially Correct | `insight_engine` | 0.95 |
| `issue-013` - Finance tool evidence contradicted | `finance-agent` | 1 | Correct | `none` | 0.99 |
| `issue-018` - Required transient retry omitted | `finance-agent` | 1 | Partially Correct | `insight_engine` | 0.99 |
| `issue-025` - Booking executes before validation and confirmation | `travel-agent` | 1 | Partially Correct | `insight_engine` | 0.94 |
| `issue-028` - Stale itinerary state reused | `travel-agent` | 3 | Partially Correct | `insight_engine` | 0.99 |
| `issue-024` - Travel inventory payload overfetched | `travel-agent` | 1 | Partially Correct | `insight_engine` | 0.94 |
| `issue-027` - Independent travel searches serialized | `travel-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-021` - Inventory fabricated after search failure | `travel-agent` | 1 | Correct | `none` | 0.99 |
| `issue-029` - Required ticket escalation omitted | `support-ticket-agent` | 1 | Incorrect | `insight_engine` | 0.98 |
| `issue-034` - Raw ticket model failure exposed | `support-ticket-agent` | 1 | Correct | `none` | 0.99 |
| `issue-033` - Ticket result omitted after successful tool | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-031` - Ticket orchestration makes no progress | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |

## Per-Agent reports

- [finance-agent](agents/finance-agent.md)
- [healthcare-agent](agents/healthcare-agent.md)
- [support-ticket-agent](agents/support-ticket-agent.md)
- [travel-agent](agents/travel-agent.md)
- [weather-agent](agents/weather-agent.md)
