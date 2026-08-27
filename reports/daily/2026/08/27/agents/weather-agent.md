# weather-agent - Insight Evaluation

- Report date: `2026-08-27`
- Run: `aiq-20260827`
- Run result: `INCOMPLETE`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 5 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 0 |
| Partially Correct | 0 |
| Incorrect | 1 |
| Noise | 4 |
| Duplicate | 0 |
| Missing expected issues | 1 |
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

| Issue | Foundry version | Generated Insight | Evaluation | Passing fields | Failing fields |
| --- | --- | --- | --- | --- | --- |
| `v0` | `8` | Weather lookup ended before tool execution | Valid Agent Finding | - | - |
| [issue-003](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-003) | `11` | Configured current-conditions tool rejected at runtime | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-003](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-003) | `11` | Instruction injection overrides weather-response policy | Incorrect | description, severity, linked traces | root cause, title, category, proposed fix |
| [issue-006](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-006) | `14` | Current-conditions request terminated without lookup or answer | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-004](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-004) | `12` | Silent post-dispatch execution loss | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-001](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-001) | `9` | Tool-execution pipeline fails to complete grounded requests | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-002](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-002) | `10` | No generated Insight | Missing | - | - |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
