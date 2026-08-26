# weather-agent - Insight Evaluation

- Report date: `2026-08-26`
- Run: `aiq-20260826-r01`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 8 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 0 |
| Partially Correct | 1 |
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
| `v0` | `1` | Execution produced no forecast response or tool call | Noise |
| [issue-006](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-006) | `7` | Current-conditions request completed with no lookup or answer | Noise |
| [issue-004](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-004) | `5` | Tool-execution lifecycle is not being completed | Noise |
| [issue-004](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-004) | `5` | Successful invocation produced no weather response | Noise |
| [issue-001](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-001) | `2` | Tool call was not executed before run ended | Noise |
| [issue-001](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-001) | `2` | Weather request completed without any response or lookup | Noise |
| [issue-002](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-002) | `3` | Successful invocation produced no weather response | Noise |
| [issue-005](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-005) | `6` | Missing tool-result finalization causes redundant calls and incomplete responses | Partially Correct |
| [issue-005](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-005) | `6` | Current-conditions request completed with no lookup or answer | Noise |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
