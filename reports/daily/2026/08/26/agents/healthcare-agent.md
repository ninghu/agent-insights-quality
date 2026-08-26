# healthcare-agent - Insight Evaluation

- Report date: `2026-08-26`
- Run: `aiq-20260826-r01`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 6 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 2 |
| Correct | 0 |
| Partially Correct | 2 |
| Incorrect | 4 |
| Noise | 0 |
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
| `v0` | `1` | Retryable slot lookup was not retried | Noise |
| `v0` | `1` | Parallel slot lookup did not produce a user-facing result | Noise |
| [issue-010](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-010) | `5` | Unverified appointment-slot availability claim | Partially Correct |
| [issue-008](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-008) | `3` | Unnecessary confirmation blocked an explicit booking | Incorrect |
| [issue-008](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-008) | `3` | Booking request produced no action or reply | Incorrect |
| [issue-007](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-007) | `2` | Handoff response uses prohibited structured ownership fields | Incorrect |
| [issue-009](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-009) | `4` | Request produced no agent action or response | Incorrect |
| [issue-012](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-012) | `7` | Tenant-boundary enforcement failure | Partially Correct |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
