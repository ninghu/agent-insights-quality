# travel-agent - Insight Evaluation

- Report date: `2026-08-28`
- Run: `aiq-20260828`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 6 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 0 |
| Correct | 2 |
| Partially Correct | 1 |
| Incorrect | 0 |
| Noise | 3 |
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

| Issue | Foundry version | Generated Insight | Evaluation | Passing fields | Failing fields |
| --- | --- | --- | --- | --- | --- |
| `v0` | `19` | No generated Insight | Correct | - | - |
| [issue-023](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-023) | `22` | Unsupported inventory availability claim | Correct | root cause, title, description, category, severity, proposed fix, linked traces | None |
| [issue-021](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-021) | `20` | Unsupported flight-seat availability claim | Correct | root cause, title, description, category, severity, proposed fix, linked traces | None |
| [issue-026](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-026) | `25` | Invented booking status from count-only search results | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-024](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-024) | `23` | Unsupported booking-status claim from count-only searches | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `27` | Search used superseded trip identifier | Partially Correct | root cause, title, description, severity, proposed fix, linked traces | category |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `27` | Invented booking status from count-only search results | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
