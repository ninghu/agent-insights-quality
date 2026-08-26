# support-ticket-agent - Insight Evaluation

- Report date: `2026-08-26`
- Run: `aiq-20260826-r01`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 2 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 2 |
| Correct | 1 |
| Partially Correct | 0 |
| Incorrect | 1 |
| Noise | 0 |
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

| Issue | Foundry version | Generated Insight | Evaluation |
| --- | --- | --- | --- |
| `v0` | `10` | History retrieval failed without recovery | Noise |
| `v0` | `10` | Ticket-read retry did not restore success | Noise |
| [issue-029](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-029) | `11` | Repeated empty-argument ticket recovery call | Incorrect |
| [issue-034](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-034) | `16` | Chat invocation fails before processing | Correct |
| [issue-033](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-033) | `15` | No generated Insight | Missing |
| [issue-030](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-030) | `12` | No generated Insight | Missing |
| [issue-031](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-031) | `13` | No generated Insight | Missing |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
