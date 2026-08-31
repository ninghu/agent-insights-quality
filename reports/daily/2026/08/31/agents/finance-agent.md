# finance-agent - Insight Evaluation

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
| Correct | 1 |
| Partially Correct | 2 |
| Incorrect | 1 |
| Noise | 1 |
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
| `v0` | `28` | Retryable balance operation was not retried | Valid Agent Finding | - | - |
| [issue-013](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-013) | `29` | Fabricated balance adjustment | Correct | root cause, title, description, category, severity, proposed fix, linked traces | None |
| [issue-020](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-020) | `36` | Duplicated conversation content inflates model context | Partially Correct | root cause, title, description, category, proposed fix | severity, linked traces |
| [issue-020](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-020) | `36` | Agent omitted required monthly-items retrieval | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-016](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-016) | `32` | Invented zero balance after account lookup failure | Partially Correct | root cause, title, description, category, severity | proposed fix, linked traces |
| [issue-015](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-015) | `31` | Mismatched balance result attributed to requested account | Incorrect | severity, linked traces | root cause, title, description, category, proposed fix |
| [issue-017](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-017) | `33` | No generated Insight | Missing | - | - |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
