# weather-agent - Insight Evaluation

- Report date: `2026-08-25`
- Run: `aiq-20260825-r01`
- Overall result: `FAIL`

| Issue | Foundry version | Generated Insight | Evaluation |
| --- | --- | --- | --- |
| `v0` | `1` | Successful invocation produced no alert lookup or answer | Noise |
| `v0` | `1` | Forecast tool call was not executed | Noise |
| `v0` | `1` | Temporary alert-tool failure was left unresolved | Noise |
| [issue-004](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-004) | `5` | Weather tool call was not executed | Noise |
| [issue-001](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-001) | `2` | Weather request terminates without lookup or answer | Noise |
| [issue-001](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-001) | `2` | Weather tool call was not executed | Noise |
| [issue-002](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-002) | `3` | Successful run contains no agent response | Noise |
| [issue-005](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-005) | `6` | Redundant successful tool-call retries | Partially Correct |
| [issue-005](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-005) | `6` | Missing end-to-end task-completion guarantees | Noise |
| [issue-003](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-003) | `4` | Tool-contract failures require capability-aware recovery | Noise |
| [issue-003](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-003) | `4` | Silent terminal failures masked as successful executions | Noise |
| [issue-003](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-003) | `4` | Formats a current-conditions request as a forecast | Partially Correct |
