# Agent Insights Quality - 2026-08-31

## Summary

| Grade | Findings |
| --- | --- |
| **INCOMPLETE** | Score **N/A** (change N/A) (PASS threshold 90/100); 1 matched, 6 partial, 2 noise cards |
| Expected issue Insights | 25 |
| Expected baseline Insights | 0 |
| Observed cards | 16 |

`field_weighted_v1`: field quality `26.6/100` at 85%; clean-card precision `87.5/100` at 15%.

## What is working

| Capability | Evidence |
| --- | --- |
| Baseline health | 0 of 5 Agents produced zero baseline Insights |
| Exact issue quality | 1 of 25 selected issues passed every field |
| Noise | 2 false-positive, unrelated, or duplicate cards |

## Baseline ownership

| Agent | Cards | Verdict | Ownership |
| --- | ---: | --- | --- |
| `weather-agent` | 0 | `inconclusive` | `unresolved` |
| `healthcare-agent` | 0 | `inconclusive` | `unresolved` |
| `finance-agent` | 1 | `agent_finding` | `agent` |
| `travel-agent` | 1 | `agent_finding` | `agent` |
| `support-ticket-agent` | 1 | `agent_finding` | `agent` |

## What needs improvement

| Issue | Agent | Result | Ownership |
| --- | --- | --- | --- |
| `issue-003` - Current conditions answer uses forecast-shaped data | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-004` - Standing units preference forgotten | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-002` - Explicit response schema violated | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-009` - Provider correction ignored | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-020` - Finance model context duplicated | `finance-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-016` - Structured finance error treated as data | `finance-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-015` - Finance tool uses wrong account scope | `finance-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-017` - Partial finance result reported complete | `finance-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-022` - Travel request routed to wrong tool | `travel-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-027` - Independent travel searches serialized | `travel-agent` | `FAIL` / Noise | `insight_engine` |
| `issue-025` - Booking executes before validation and confirmation | `travel-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-023` - Required inventory search omitted | `travel-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-021` - Inventory fabricated after search failure | `travel-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-036` - One ticket defect fragments into multiple cards | `support-ticket-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-034` - Raw ticket model failure exposed | `support-ticket-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-035` - Ticket operation reports false success | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | `FAIL` / Incorrect | `insight_engine` |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |

## Human validation

| Issue | Agent | Cards | Sol verdict | Ownership | Confidence |
| --- | --- | ---: | --- | --- | ---: |
| `issue-003` - Current conditions answer uses forecast-shaped data | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-004` - Standing units preference forgotten | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-002` - Explicit response schema violated | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-009` - Provider correction ignored | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-013` - Finance tool evidence contradicted | `finance-agent` | 1 | Correct | `none` | 0.99 |
| `issue-020` - Finance model context duplicated | `finance-agent` | 2 | Partially Correct | `insight_engine` | 0.98 |
| `issue-016` - Structured finance error treated as data | `finance-agent` | 1 | Partially Correct | `insight_engine` | 0.97 |
| `issue-015` - Finance tool uses wrong account scope | `finance-agent` | 1 | Incorrect | `insight_engine` | 0.98 |
| `issue-017` - Partial finance result reported complete | `finance-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-022` - Travel request routed to wrong tool | `travel-agent` | 1 | Partially Correct | `insight_engine` | 0.99 |
| `issue-027` - Independent travel searches serialized | `travel-agent` | 1 | Noise | `insight_engine` | 0.99 |
| `issue-025` - Booking executes before validation and confirmation | `travel-agent` | 1 | Incorrect | `insight_engine` | 0.98 |
| `issue-023` - Required inventory search omitted | `travel-agent` | 1 | Partially Correct | `insight_engine` | 0.99 |
| `issue-021` - Inventory fabricated after search failure | `travel-agent` | 1 | Partially Correct | `insight_engine` | 0.99 |
| `issue-036` - One ticket defect fragments into multiple cards | `support-ticket-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-034` - Raw ticket model failure exposed | `support-ticket-agent` | 1 | Partially Correct | `insight_engine` | 0.96 |
| `issue-035` - Ticket operation reports false success | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | 1 | Incorrect | `insight_engine` | 0.99 |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |

## Per-Agent reports

- [finance-agent](agents/finance-agent.md)
- [healthcare-agent](agents/healthcare-agent.md)
- [support-ticket-agent](agents/support-ticket-agent.md)
- [travel-agent](agents/travel-agent.md)
- [weather-agent](agents/weather-agent.md)
