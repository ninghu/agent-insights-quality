# Insight Result Labels

Each generated Agent Insight card is evaluated independently against one reviewed expected issue.
These labels describe whether the card is useful and correct; they are not based on a fixed count of
passing fields.

| Label | Meaning | Generated finding type |
| --- | --- | --- |
| Fully Correct | The card identifies the expected root cause and passes title, description, category, severity, proposed fix, and linked-trace checks. | `MATCHED` |
| Partially Correct | The card identifies the correct problem direction and remains useful, but one or more fields are incomplete or inaccurate. | `PARTIAL` |
| Incorrect | The card is related to the tested issue, but its root cause or other material guidance is wrong or misleading. | `MISMATCHED` |
| Noise / Duplicate | The card should not have appeared because it is unrelated, a false positive, an extra duplicate, or a finding on a healthy baseline. | `NOISE` or `DUPLICATE` |

## How field results are read

Every attributable card has a separate pass/fail result for root cause, title, description, category,
severity, proposed fix, and linked traces. The label is a holistic usefulness judgment, not a formula
based only on the number of passing fields. The per-Agent report contains the actual field results for
each card.

Example Partially Correct card:

| Field | Result |
| --- | --- |
| Root cause | Pass |
| Title | Pass |
| Description | Pass |
| Category | Pass |
| Severity | Fail |
| Proposed fix | Fail |
| Linked traces | Pass |

The diagnosis is still useful because the root cause and problem direction are correct.

Example Incorrect card:

| Field | Result |
| --- | --- |
| Root cause | Fail |
| Title | Pass |
| Description | Pass |
| Category | Fail |
| Severity | Pass |
| Proposed fix | Fail |
| Linked traces | Pass |

Although the title and description discuss the right topic, the wrong root cause and remediation would
mislead an engineer. These are examples only; actual field combinations vary by card.

## Fully Correct

A card is Fully Correct only when all seven reviewed fields pass:

1. root cause;
2. title;
3. description;
4. category;
5. severity;
6. proposed fix;
7. linked traces.

## Partially Correct

A Partially Correct card points to the real defect and provides useful diagnostic signal, but it does
not meet the strict full-quality bar. For example, the root cause can be correct while the severity is
wrong or the proposed fix is incomplete.

## Incorrect

An Incorrect card is not merely incomplete. Its root cause or another material claim would lead an
engineer toward the wrong understanding or remediation. A related title or description does not make
the card partially correct when its core diagnosis is wrong.

## Noise / Duplicate

Noise is not a lower-quality description of the expected issue. It is a card that should not exist at
all. This includes false positives, unrelated findings, redundant duplicates, and findings generated
for a healthy baseline.

## Missing expected Insight

`MISSING` is reported separately when complete endpoint and trace evidence proves an expected issue,
but Agent Insights generates no attributable card.

## Scoring

Fully Correct and Partially Correct cards retain the field credit they earn. Incorrect cards receive
credit only for individual fields that are actually correct. Noise and extra duplicates reduce the
clean-card precision component. See [Quality Bar](QUALITY_BAR.md) for the complete formula.
