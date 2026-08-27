# travel-agent - Insight Evaluation

- Report date: `2026-08-27`
- Run: `aiq-20260827`
- Run result: `INCOMPLETE`

## Review summary

| Metric | Value |
| --- | ---: |
| Expected issue Insights | 5 |
| Generated issue cards | 7 |
| Expected baseline Insights | 0 |
| Generated baseline cards | 0 |
| Correct | 1 |
| Partially Correct | 0 |
| Incorrect | 4 |
| Noise | 2 |
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
| `v0` | `10` | No generated Insight | Correct | - | - |
| [issue-023](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-023) | `13` | Unsupported flight inventory claim | Incorrect | root cause, title, description, severity, proposed fix, linked traces | category |
| [issue-022](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-022) | `12` | Hotel-search result misrepresented as flight option | Incorrect | root cause, title, description, proposed fix, linked traces | category, severity |
| [issue-026](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-026) | `16` | Flight comparison lacks both trip results | Incorrect | root cause, title, description, category, proposed fix, linked traces | severity |
| [issue-025](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-025) | `15` | Claimed booking despite explicit prohibition | Incorrect | category, severity, linked traces | root cause, title, description, proposed fix |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `18` | Claims booking after search-only action | Noise | severity | root cause, title, description, category, proposed fix, linked traces |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `18` | Trip switch instruction ignored | Correct | root cause, title, description, category, severity, proposed fix, linked traces | None |
| [issue-028](https://github.com/ninghu/agent-insights-quality/blob/main/ISSUE_CATALOG.md#issue-028) | `18` | Comparison request reduced to option count | Noise | None | root cause, title, description, category, severity, proposed fix, linked traces |

## Human validation checklist

- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.
- [ ] Confirm the Foundry version matches the version under review.
- [ ] Compare every generated card with the linked issue definition.
- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.
- [ ] Confirm every expected issue without a card is labeled Missing.
- [ ] Open the Agent from the email and inspect linked traces for disputed cards.
- [ ] Record reviewer agree/disagree decisions outside this generated report.
