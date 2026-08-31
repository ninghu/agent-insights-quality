# travel-agent - Insight Evaluation

- Report date: `2026-08-31`
- Run: `aiq-20260831`
- Run result: `INCOMPLETE`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 5 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 0 |
| Partially Correct | 3 |
| Incorrect | 1 |
| Noise | 1 |
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
| `v0` | `19` | Flight search suppressed option details | Valid Agent Finding | - | - |
| [issue-022](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-022) | `21` | Flight request routed to hotel search | Partially Correct | root cause, title, description, category, proposed fix, linked traces | severity |
| [issue-027](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-027) | `26` | Searches omit data needed for comparison | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-025](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-025) | `24` | Claims a flight was booked despite explicit no-booking instruction | Incorrect | category, severity, linked traces | root cause, title, description, proposed fix |
| [issue-023](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-023) | `22` | Unsupported inventory availability claim | Partially Correct | root cause, title, description, category, proposed fix, linked traces | severity |
| [issue-021](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-021) | `20` | Unsupported flight-seat availability claim | Partially Correct | root cause, title, description, category, proposed fix, linked traces | severity |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
