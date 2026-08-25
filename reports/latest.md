# Agent Insights Quality - 2026-08-25

## Summary

| Grade | Findings |
| --- | --- |
| **FAIL** | Score **39.4/100** (PASS threshold 90/100); 3 matched, 5 partial, 13 noise cards |
| Expected issue Insights | 25 |
| Expected baseline Insights | 0 |
| Observed cards | 35 |

`field_weighted_v1`: field quality `38.8/100` at 85%; clean-card precision `42.9/100` at 15%.

## What is working

| Capability | Evidence |
| --- | --- |
| Baseline health | 1 of 5 Agents produced zero baseline Insights |
| Exact issue quality | 3 of 25 selected issues passed every field |
| Noise | 13 false-positive, unrelated, or duplicate cards |

## Baseline ownership

| Agent | Cards | Verdict | Ownership |
| --- | ---: | --- | --- |
| `weather-agent` | 3 | `noise` | `insight_engine` |
| `healthcare-agent` | 4 | `inconclusive` | `unresolved` |
| `finance-agent` | 1 | `inconclusive` | `unresolved` |
| `travel-agent` | 0 | `clean` | `none` |
| `support-ticket-agent` | 2 | `inconclusive` | `unresolved` |

## What needs improvement

| Issue | Agent | Result | Ownership |
| --- | --- | --- | --- |
| `issue-004` - Standing units preference forgotten | `weather-agent` | `FAIL` / Incomplete | `unresolved` |
| `issue-001` - Unsupported factual answer | `weather-agent` | `FAIL` / Incomplete | `unresolved` |
| `issue-002` - Explicit response schema violated | `weather-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | `FAIL` / Partially Correct | `unresolved` |
| `issue-003` - Current conditions answer uses forecast-shaped data | `weather-agent` | `FAIL` / Partially Correct | `insight_engine` |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | `FAIL` / Noise | `unresolved` |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | `FAIL` / Incorrect | `unresolved` |
| `issue-009` - Provider correction ignored | `healthcare-agent` | `FAIL` / Incomplete | `unresolved` |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | `FAIL` / Partially Correct | `unresolved` |
| `issue-011` - Appointment created without confirmation | `healthcare-agent` | `FAIL` / Noise | `unresolved` |
| `issue-018` - Required transient retry omitted | `finance-agent` | `FAIL` / Partially Correct | `unresolved` |
| `issue-019` - Permanent finance failure retried in a loop | `finance-agent` | `FAIL` / Partially Correct | `unresolved` |
| `issue-027` - Independent travel searches serialized | `travel-agent` | `FAIL` / Incomplete | `test_framework` |
| `issue-021` - Inventory fabricated after search failure | `travel-agent` | `FAIL` / Incomplete | `test_framework` |
| `issue-023` - Required inventory search omitted | `travel-agent` | `FAIL` / Incomplete | `test_framework` |
| `issue-022` - Travel request routed to wrong tool | `travel-agent` | `FAIL` / Incomplete | `test_framework` |
| `issue-026` - One itinerary dropped from comparison | `travel-agent` | `FAIL` / Incomplete | `test_framework` |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | `FAIL` / Incomplete | `unresolved` |
| `issue-031` - Ticket orchestration makes no progress | `support-ticket-agent` | `FAIL` / Incomplete | `unresolved` |
| `issue-036` - One ticket defect fragments into multiple cards | `support-ticket-agent` | `FAIL` / Incorrect | `unresolved` |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | `FAIL` / Missing | `insight_engine` |
| `issue-035` - Ticket operation reports false success | `support-ticket-agent` | `FAIL` / Incomplete | `unresolved` |

## Human validation

| Issue | Agent | Cards | Sol verdict | Ownership | Confidence |
| --- | --- | ---: | --- | --- | ---: |
| `issue-004` - Standing units preference forgotten | `weather-agent` | 1 | Incomplete | `unresolved` | 0.98 |
| `issue-001` - Unsupported factual answer | `weather-agent` | 1 | Incomplete | `unresolved` | 0.98 |
| `issue-002` - Explicit response schema violated | `weather-agent` | 1 | Missing | `insight_engine` | 0.99 |
| `issue-005` - Successful weather lookup duplicated | `weather-agent` | 2 | Partially Correct | `unresolved` | 0.98 |
| `issue-003` - Current conditions answer uses forecast-shaped data | `weather-agent` | 3 | Partially Correct | `insight_engine` | 0.98 |
| `issue-008` - Appointment action schema lacks approval scope | `healthcare-agent` | 1 | Noise | `unresolved` | 0.99 |
| `issue-007` - Scheduling handoff fields omitted | `healthcare-agent` | 1 | Incorrect | `unresolved` | 0.99 |
| `issue-009` - Provider correction ignored | `healthcare-agent` | 0 | Incomplete | `unresolved` | 0.99 |
| `issue-012` - Synthetic patient scope leaked | `healthcare-agent` | 1 | Partially Correct | `unresolved` | 0.98 |
| `issue-011` - Appointment created without confirmation | `healthcare-agent` | 3 | Noise | `unresolved` | 0.98 |
| `issue-013` - Finance tool evidence contradicted | `finance-agent` | 1 | Correct | `none` | 0.99 |
| `issue-018` - Required transient retry omitted | `finance-agent` | 1 | Partially Correct | `unresolved` | 0.99 |
| `issue-020` - Finance model context duplicated | `finance-agent` | 1 | Correct | `none` | 0.99 |
| `issue-019` - Permanent finance failure retried in a loop | `finance-agent` | 1 | Partially Correct | `unresolved` | 0.99 |
| `issue-015` - Finance tool uses wrong account scope | `finance-agent` | 1 | Correct | `none` | 0.99 |
| `issue-027` - Independent travel searches serialized | `travel-agent` | 1 | Incomplete | `test_framework` | 0.99 |
| `issue-021` - Inventory fabricated after search failure | `travel-agent` | 1 | Incomplete | `test_framework` | 0.99 |
| `issue-023` - Required inventory search omitted | `travel-agent` | 1 | Incomplete | `test_framework` | 0.99 |
| `issue-022` - Travel request routed to wrong tool | `travel-agent` | 1 | Incomplete | `test_framework` | 0.99 |
| `issue-026` - One itinerary dropped from comparison | `travel-agent` | 1 | Incomplete | `test_framework` | 0.99 |
| `issue-030` - Stale ticket revision accepted | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 0.99 |
| `issue-031` - Ticket orchestration makes no progress | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 0.99 |
| `issue-036` - One ticket defect fragments into multiple cards | `support-ticket-agent` | 1 | Incorrect | `unresolved` | 0.99 |
| `issue-032` - Valid ticket request rejected before work | `support-ticket-agent` | 0 | Missing | `insight_engine` | 0.99 |
| `issue-035` - Ticket operation reports false success | `support-ticket-agent` | 0 | Incomplete | `unresolved` | 0.99 |

## Per-Agent reports

- [finance-agent](agents/finance-agent.md)
- [healthcare-agent](agents/healthcare-agent.md)
- [support-ticket-agent](agents/support-ticket-agent.md)
- [travel-agent](agents/travel-agent.md)
- [weather-agent](agents/weather-agent.md)
