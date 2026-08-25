# healthcare-agent - Insight Evaluation

- Report date: `2026-08-25`
- Run: `aiq-20260825-r01`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 6 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 4 |
| Correct | 0 |
| Partially Correct | 2 |
| Incorrect | 2 |
| Noise | 2 |
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

| Issue | Foundry version | Generated Insight | Evaluation |
| --- | --- | --- | --- |
| `v0` | `1` | Tool-call continuation was not completed | Incomplete |
| `v0` | `1` | Parallel lookup results were not rendered | Incomplete |
| `v0` | `1` | Retryable slot lookup was not retried | Incomplete |
| `v0` | `1` | Slot search completed without lookup or answer | Incomplete |
| [issue-008](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-008) | `3` | Booking request completed with no agent action | Noise |
| [issue-007](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-007) | `2` | Invented handoff completion deadline | Incorrect |
| [issue-009](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-009) | `4` | No generated Insight | Incomplete |
| [issue-012](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-012) | `7` | Tenant-boundary enforcement failure | Partially Correct |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `6` | Slot-details request misrouted to appointment creation | Incorrect |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `6` | Unsupported suitability claim without availability lookup | Noise |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `6` | Tentative slot mention treated as booking confirmation | Partially Correct |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
