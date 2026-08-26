# finance-agent - Insight Evaluation

- Report date: `2026-08-26`
- Run: `aiq-20260826-r01`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 5 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 2 |
| Correct | 2 |
| Partially Correct | 3 |
| Incorrect | 0 |
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
| `v0` | `10` | Transient balance tool failed on its promised retry | Noise |
| `v0` | `10` | Redundant detail lookups for unknown account | Noise |
| [issue-017](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-017) | `15` | Aggregate mislabeled complete after items failure | Partially Correct |
| [issue-014](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-014) | `12` | Balance lookup omitted the supplied account ID | Correct |
| [issue-016](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-016) | `14` | Tool-error continuation ended without a user-facing response | Partially Correct |
| [issue-013](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-013) | `11` | Reported balance does not match authoritative tool result | Correct |
| [issue-018](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-018) | `16` | Retryable transient balance error was not retried | Partially Correct |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
