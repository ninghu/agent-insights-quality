# Insight Result Labels

Each generated Agent Insight card is evaluated independently against one reviewed expected issue.
Reports use five public result categories.

| Result | Meaning |
| --- | --- |
| Correct | At least one attributable card passes title, description, category, and linked traces. |
| Incorrect | An attributable card exists, but no card passes all four scoring fields. |
| Missing | No attributable card covers the expected issue. |
| Noise | An extra card is unrelated to every expected issue or contradicted by independent evidence. |
| Duplicate | An extra card repeats an attributable card for the same expected issue. |

## Scoring and diagnostic fields

Every attributable card is assessed on six native Insight fields:

| Field | Affects score |
| --- | --- |
| Title | Yes |
| Description | Yes |
| Category | Yes |
| Linked traces | Yes |
| Severity | No |
| Proposed fix | No |

Severity and proposed fix remain visible so engineers can improve them, but either may be wrong
without changing a Correct result.

Linked traces pass when at least one exact-run, exact-version linked trace independently supports the
card's core conclusion. Extra links do not fail the field unless they use the wrong run or version,
or contradict the conclusion.

## Incorrect versus Noise

Incorrect and Noise are deliberately separate:

- Incorrect is related to the expected issue but has a material scoring-field error.
- Noise does not identify the expected issue at all.

For example, if the expected issue is that a Finance Agent used the wrong account, an Insight that
identifies the account-scope problem but links contradictory evidence is Incorrect. An Insight about
response latency is Noise; the account-scope issue is also Missing.

## Baseline ownership

`v0` source and configuration are reviewed to contain no injected defect. Runtime can still deviate
from that contract:

- `agent`: model or workflow behavior violated the healthy contract;
- `insight_engine`: independent evidence proves healthy behavior but the card is a false positive;
- `test_framework`: fixture, dispatch, correlation, or evidence extraction is wrong;
- `infrastructure`: identity, quota, service availability, deployment, or ingestion failed;
- `unresolved`: retained evidence cannot distinguish the owner.

A card generated for `v0` is Noise only when independent runtime evidence contradicts it. If
independent trace proof shows that `v0` violated its reviewed healthy contract, the card is a valid
Agent finding.

## Scoring

Correct issues contribute to the numerator. Incorrect and Missing remain inside the fixed expected
issue count. Noise and Duplicate cards are extra output and expand the denominator. See
[Quality Score](QUALITY_BAR.md) for the complete formula.
