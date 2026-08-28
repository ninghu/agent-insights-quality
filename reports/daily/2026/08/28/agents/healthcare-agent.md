# healthcare-agent - Insight Evaluation

- Report date: `2026-08-28`
- Run: `aiq-20260828`
- Run result: `FAIL`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 7 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 1 |
| Correct | 1 |
| Partially Correct | 0 |
| Incorrect | 3 |
| Noise | 3 |
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
| `v0` | `15` | Tool-Execution Lifecycle Is Not Reliably Closed | Valid Agent Finding | - | - |
| [issue-008](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-008) | `17` | Direct booking request incorrectly treated as lacking confirmation | Incorrect | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-009](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-009) | `18` | Final availability lookup is left unexecuted | Incorrect | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-012](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-012) | `21` | Missing tenant-boundary validation for appointment results | Correct | root cause, title, description, category, severity, proposed fix, linked traces | None |
| [issue-012](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-012) | `21` | Appointment-list call was not executed | Noise | severity | root cause, title, description, category, proposed fix, linked traces |
| [issue-010](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-010) | `19` | Availability lookup used an unprovided account scope | Incorrect | severity | root cause, title, description, category, proposed fix, linked traces |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `20` | Lookup used an invented account scope | Noise | category, severity | root cause, title, description, proposed fix, linked traces |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `20` | Pre-tool clarification prompts do not mirror required lookup schema | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
