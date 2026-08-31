# support-ticket-agent - Insight Evaluation

- Report date: `2026-08-31`
- Run: `aiq-20260831`
- Run result: `INCOMPLETE`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 3 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 0 |
| Partially Correct | 1 |
| Incorrect | 2 |
| Noise | 0 |
| Duplicate | 0 |
| Missing expected issues | 2 |
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
| `v0` | `19` | Unknown read-tool errors lack robust recovery handling | Valid Agent Finding | - | - |
| [issue-036](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-036) | `27` | Ticket workflow invokes both tools without arguments | Incorrect | linked traces | root cause, title, description, category, severity, proposed fix |
| [issue-034](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-034) | `25` | Chat invocation fails before execution | Partially Correct | category, severity, proposed fix, linked traces | root cause, title, description |
| [issue-035](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-035) | `26` | No generated Insight | Missing | - | - |
| [issue-030](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-030) | `21` | Empty ticket update invocation | Incorrect | severity, linked traces | root cause, title, description, category, proposed fix |
| [issue-032](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-032) | `23` | No generated Insight | Missing | - | - |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
