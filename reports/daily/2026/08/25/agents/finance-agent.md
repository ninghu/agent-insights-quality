# finance-agent - Insight Evaluation

- Report date: `2026-08-25`
- Run: `aiq-20260825-r01`
- Overall result: `FAIL`

| Issue | Foundry version | Generated Insight | Evaluation |
| --- | --- | --- | --- |
| `v0` | `1` | Transient balance tool violated its retry contract | Incomplete |
| [issue-013](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-013) | `2` | Final balance contradicts tool result | Correct |
| [issue-018](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-018) | `7` | Retryable transient balance error was not retried | Partially Correct |
| [issue-020](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-020) | `9` | Repeated conversation replay bloats final model input | Correct |
| [issue-019](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-019) | `8` | Repeated retries after non-retryable account lookup failure | Partially Correct |
| [issue-015](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-015) | `4` | Balance lookup used the prohibited account | Correct |
