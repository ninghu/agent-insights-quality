# healthcare-agent - Insight Evaluation

- Report date: `2026-08-27`
- Run: `aiq-20260827`
- Run result: `INCOMPLETE`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 4 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 3 |
| Correct | 0 |
| Partially Correct | 0 |
| Incorrect | 1 |
| Noise | 3 |
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
| `v0` | `8` | Availability request completed without slot lookup | Valid Agent Finding | - | - |
| `v0` | `8` | Temporary slot-lookup error ends without a user response | Valid Agent Finding | - | - |
| `v0` | `8` | Parallel lookup lacks recorded final summary | Valid Agent Finding | - | - |
| [issue-011](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-011) | `13` | Slot-detail request gathers insufficient lookup inputs | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |
| [issue-010](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-010) | `12` | No generated Insight | Incomplete | - | - |
| [issue-008](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-008) | `10` | Silent execution-path failures for actionable requests | Noise | severity | root cause, title, description, category, proposed fix, linked traces |
| [issue-007](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-007) | `9` | Handoff includes prohibited structured owner field | Incorrect | category | root cause, title, description, severity, proposed fix, linked traces |
| [issue-009](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-009) | `11` | Silent turn-processing failure across scheduling workflow | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
