# support-ticket-agent - Insight Evaluation

- Report date: `2026-08-28`
- Run: `aiq-20260828`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 1 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 0 |
| Partially Correct | 0 |
| Incorrect | 1 |
| Noise | 0 |
| Duplicate | 0 |
| Missing expected issues | 4 |
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
| `v0` | `19` | Dispatch lacks resilient recovery for prerequisite data retrieval failures | Noise | - | - |
| [issue-030](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-030) | `21` | No generated Insight | Missing | - | - |
| [issue-032](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-032) | `23` | No generated Insight | Missing | - | - |
| [issue-033](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-033) | `24` | No generated Insight | Missing | - | - |
| [issue-029](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-029) | `20` | Unconditional retry of failed ticket recovery | Incorrect | severity, linked traces | root cause, title, description, category, proposed fix |
| [issue-031](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-031) | `22` | No generated Insight | Missing | - | - |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
