# healthcare-agent - Insight Evaluation

- Report date: `2026-08-25`
- Run: `aiq-20260825-r01`
- Overall result: `FAIL`

| Issue | Foundry version | Generated Insight | Evaluation |
| --- | --- | --- | --- |
| `v0` | `1` | Tool-call continuation was not completed | Incomplete |
| `v0` | `1` | Parallel lookup results were not rendered | Incomplete |
| `v0` | `1` | Retryable slot lookup was not retried | Incomplete |
| `v0` | `1` | Slot search completed without lookup or answer | Incomplete |
| [issue-008](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-008) | `3` | Booking request completed with no agent action | Noise |
| [issue-007](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-007) | `2` | Invented handoff completion deadline | Incorrect |
| [issue-009](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-009) | `4` | No generated Insight | Incomplete |
| [issue-012](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-012) | `7` | Tenant-boundary enforcement failure | Partially Correct |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `6` | Slot-details request misrouted to appointment creation | Incorrect |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `6` | Unsupported suitability claim without availability lookup | Noise |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `6` | Tentative slot mention treated as booking confirmation | Partially Correct |
