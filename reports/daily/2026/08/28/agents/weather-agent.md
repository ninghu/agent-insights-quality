# weather-agent - Insight Evaluation

- Report date: `2026-08-28`
- Run: `aiq-20260828`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 3 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 0 |
| Partially Correct | 2 |
| Incorrect | 0 |
| Noise | 1 |
| Duplicate | 0 |
| Missing expected issues | 3 |
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
| `v0` | `15` | Weather lookup stopped at tool-call emission | Valid Agent Finding | - | - |
| [issue-006](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-006) | `21` | Terminal-response duplication undermines output-constraint compliance | Partially Correct | title, severity, proposed fix | root cause, description, category, linked traces |
| [issue-006](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-006) | `21` | Emitted weather tool calls are not dispatched or resumed to a final answer | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-005](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-005) | `20` | Unreliable tool-use orchestration and completion control | Partially Correct | proposed fix, linked traces | root cause, title, description, category, severity |
| [issue-004](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-004) | `19` | No generated Insight | Missing | - | - |
| [issue-002](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-002) | `17` | No generated Insight | Missing | - | - |
| [issue-001](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-001) | `16` | No generated Insight | Missing | - | - |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
