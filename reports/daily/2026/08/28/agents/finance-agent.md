# finance-agent - Insight Evaluation

- Report date: `2026-08-28`
- Run: `aiq-20260828`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 6 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 2 |
| Partially Correct | 1 |
| Incorrect | 1 |
| Noise | 1 |
| Duplicate | 1 |
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
| `v0` | `28` | Tool result was not converted into a user response | Valid Agent Finding | - | - |
| [issue-015](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-015) | `31` | Agent queried the opposite account | Correct | root cause, title, description, category, severity, proposed fix, linked traces | None |
| [issue-015](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-015) | `31` | Tool-output identity mismatches are being rationalized instead of handled as data-integrity failures | Duplicate | severity, proposed fix, linked traces | root cause, title, description, category |
| [issue-017](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-017) | `33` | Tool results were never delivered to the user | Incorrect | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-014](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-014) | `30` | No generated Insight | Missing | - | - |
| [issue-019](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-019) | `35` | Agent retried a non-retryable account lookup | Partially Correct | root cause, title, description, severity, proposed fix | category, linked traces |
| [issue-019](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-019) | `35` | Tool failure was not turned into a user response | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-018](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-018) | `34` | Retryable tool failures are not handled according to recovery contracts | Correct | root cause, title, description, category, severity, proposed fix, linked traces | None |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
