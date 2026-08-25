# Agent Insights Quality - 2026-08-24

## Summary

| Grade | Findings |
| --- | --- |
| **INCOMPLETE** | Score **N/A** (PASS threshold 90/100); 0 matched, 0 partial, 0 noise cards |
| Expected issue Insights | 25 |
| Expected baseline Insights | 0 |
| Observed cards | 0 |

`field_weighted_v1`: field quality `0/100` at 85%; clean-card precision `0/100` at 15%.

## What is working

| Capability | Evidence |
| --- | --- |
| Baseline health | 0 of 5 Agents produced zero baseline Insights |
| Exact issue quality | 0 of 25 selected issues passed every field |
| Noise | 0 false-positive, unrelated, or duplicate cards |

## Baseline ownership

| Agent | Cards | Verdict | Ownership |
| --- | ---: | --- | --- |
| `weather-agent` | 0 | `inconclusive` | `unresolved` |
| `healthcare-agent` | 0 | `inconclusive` | `unresolved` |
| `finance-agent` | 0 | `inconclusive` | `unresolved` |
| `travel-agent` | 0 | `inconclusive` | `unresolved` |
| `support-ticket-agent` | 0 | `inconclusive` | `unresolved` |

## What needs improvement

| Issue | Agent | Result | Ownership |
| --- | --- | --- | --- |
| `issue-001` - Unsupported factual answer | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-002` - Explicit response schema violated | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-003` - Current conditions answer uses forecast-shaped data | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-009` - Provider correction ignored | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-011` - Appointment created without confirmation | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-019` - Permanent finance failure retried in a loop | `finance-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-015` - Finance tool uses wrong account scope | `finance-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-017` - Partial finance result reported complete | `finance-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-014` - Required account identifier omitted | `finance-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-016` - Structured finance error treated as data | `finance-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-022` - Travel request routed to wrong tool | `travel-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-026` - One itinerary dropped from comparison | `travel-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-025` - Booking executes before validation and confirmation | `travel-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-028` - Stale itinerary state reused | `travel-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-024` - Travel inventory payload overfetched | `travel-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-035` - Ticket operation reports false success | `support-ticket-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-029` - Required ticket escalation omitted | `support-ticket-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-034` - Raw ticket model failure exposed | `support-ticket-agent` | `INCOMPLETE` / Incomplete | `unresolved` |
| `issue-033` - Ticket result omitted after successful tool | `support-ticket-agent` | `INCOMPLETE` / Incomplete | `unresolved` |

## Human validation

| Issue | Agent | Cards | Sol verdict | Ownership | Confidence |
| --- | --- | ---: | --- | --- | ---: |
| `issue-001` - Unsupported factual answer | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-002` - Explicit response schema violated | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-003` - Current conditions answer uses forecast-shaped data | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-006` - Concise weather answer overgenerated | `weather-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-009` - Provider correction ignored | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-011` - Appointment created without confirmation | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-010` - Slot availability claimed without lookup | `healthcare-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-019` - Permanent finance failure retried in a loop | `finance-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-015` - Finance tool uses wrong account scope | `finance-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-017` - Partial finance result reported complete | `finance-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-014` - Required account identifier omitted | `finance-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-016` - Structured finance error treated as data | `finance-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-022` - Travel request routed to wrong tool | `travel-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-026` - One itinerary dropped from comparison | `travel-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-025` - Booking executes before validation and confirmation | `travel-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-028` - Stale itinerary state reused | `travel-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-024` - Travel inventory payload overfetched | `travel-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-035` - Ticket operation reports false success | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-029` - Required ticket escalation omitted | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-034` - Raw ticket model failure exposed | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 1.00 |
| `issue-033` - Ticket result omitted after successful tool | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 1.00 |

## Per-Agent reports

- [finance-agent](agents/finance-agent.md)
- [healthcare-agent](agents/healthcare-agent.md)
- [support-ticket-agent](agents/support-ticket-agent.md)
- [travel-agent](agents/travel-agent.md)
- [weather-agent](agents/weather-agent.md)
