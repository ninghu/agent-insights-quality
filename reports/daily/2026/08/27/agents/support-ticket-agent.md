# support-ticket-agent - Insight Evaluation

- Report date: `2026-08-27`
- Run: `aiq-20260827`
- Run result: `INCOMPLETE`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 3 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 2 |
| Correct | 0 |
| Partially Correct | 0 |
| Incorrect | 3 |
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
| `v0` | `10` | Recovered ticket-read retry still leaves run failed | Valid Agent Finding | - | - |
| `v0` | `10` | History retrieval terminates with unknown tool error | Valid Agent Finding | - | - |
| [issue-036](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-036) | `18` | Both ticket tools fail with empty invocation arguments | Incorrect | linked traces | root cause, title, description, category, severity, proposed fix |
| [issue-032](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-032) | `14` | No generated Insight | Incomplete | - | - |
| [issue-035](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-035) | `17` | No generated Insight | Missing | - | - |
| [issue-029](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-029) | `11` | Repeated failed recover_ticket invocation | Incorrect | linked traces | root cause, title, description, category, severity, proposed fix |
| [issue-034](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-034) | `16` | Chat dispatch fails before model execution | Incorrect | category, severity, proposed fix, linked traces | root cause, title, description |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
