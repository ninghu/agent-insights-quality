# travel-agent - Insight Evaluation

- Report date: `2026-08-24`
- Run: `aiq-20260824`
- Run result: `INCOMPLETE`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 0 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 0 |
| Correct | 0 |
| Partially Correct | 0 |
| Incorrect | 0 |
| Noise | 0 |
| Duplicate | 0 |
| Missing expected issues | 5 |
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
| `v0` | `1` | No generated Insight | Correct |
| [issue-022](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-022) | `3` | No generated Insight | Incomplete |
| [issue-026](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-026) | `7` | No generated Insight | Incomplete |
| [issue-025](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-025) | `6` | No generated Insight | Incomplete |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `9` | No generated Insight | Incomplete |
| [issue-024](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-024) | `5` | No generated Insight | Incomplete |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
