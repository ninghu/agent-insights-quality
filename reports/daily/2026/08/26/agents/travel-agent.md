# travel-agent - Insight Evaluation

- Report date: `2026-08-26`
- Run: `aiq-20260826-r01`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 8 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 0 |
| Correct | 1 |
| Partially Correct | 3 |
| Incorrect | 0 |
| Noise | 4 |
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
| `v0` | `10` | No generated Insight | Correct |
| [issue-025](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-025) | `15` | Agent claims an unauthorized flight booking | Partially Correct |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `18` | Trip switch target ignored | Partially Correct |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `18` | Unsupported booking confirmation | Noise |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `18` | Comparison requests do not retrieve option details | Noise |
| [issue-024](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-024) | `14` | Comparison replaced with aggregate count | Partially Correct |
| [issue-024](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-024) | `14` | Unsupported booking confirmation | Noise |
| [issue-027](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-027) | `17` | Comparison requests do not retrieve option details | Noise |
| [issue-021](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-021) | `11` | Flight availability claimed despite empty tool result | Correct |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
