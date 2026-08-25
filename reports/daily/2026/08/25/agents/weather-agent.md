# weather-agent - Insight Evaluation

- Report date: `2026-08-25`
- Run: `aiq-20260825-r01`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 9 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 3 |
| Correct | 0 |
| Partially Correct | 2 |
| Incorrect | 0 |
| Noise | 7 |
| Duplicate | 0 |
| Missing expected issues | 0 |
| Incomplete card evaluations | 0 |

## Evaluation guide

- **Correct:** the card matches the expected issue and every required field.
- **Partially Correct:** the card is useful and related, but one or more fields are wrong.
- **Incorrect:** the card is related but materially misstates the issue.
- **Noise:** the card is unrelated or a false positive.
- **Duplicate:** an extra card represents an expected root already covered by another card.
- **Missing:** no generated card represents the expected issue.
- **Incomplete:** available evidence cannot support a reliable card judgment.

## Insight-level evaluation

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

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
