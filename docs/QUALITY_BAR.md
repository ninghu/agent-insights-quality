# Quality Bar

## PASS

A complete daily or staging run is `PASS` when its quality score is at least `90/100`.

## Quality score

The reviewed `field_weighted_v1` formula is:

```text
field_quality = average best attributable card's weighted field score for every expected issue
clean_card_precision = (observed cards - noise and extra duplicates) / observed cards
score = 0.85 * field_quality + 0.15 * clean_card_precision
```

- Field weights are root cause 25%; title 10%; description 15%; category 10%; severity 10%; proposed
  fix 15%; and linked traces 15%.
- A missing expected issue receives zero field quality.
- Noise and extra duplicates reduce the clean-card precision component, which contributes 15% of the
  final score.
- Daily uses 25 expected issues; full staging uses 36.
- Ownership does not change the score. It identifies whether remediation belongs to the Agent, Insight
  Engine, test framework, infrastructure, or remains unresolved.
- A trace-proven valid Agent finding on `v0` is not Insight noise and does not reduce clean-card
  precision. It is reported separately as a baseline health failure.

The report stores both component scores and the final score to one decimal place when needed.
Daily reports show the signed score change against the most recent prior daily report with a numeric
score. Incomplete reports are skipped as comparison baselines, and an incomplete current run shows
the score change as `N/A`.
Structural, privacy, provenance, and
reporting evidence must still be complete and trustworthy before a score can be used.
See [Insight Result Labels](INSIGHT_RESULTS.md) for Fully Correct, Partially Correct, Incorrect, and
Noise definitions and field examples.

## Staging shadow calibration

`coverage_quality_precision_v2` is a report-only policy candidate. It does not change the official
`field_weighted_v1` score, `90/100` threshold, report status, promotion authority, daily trend,
or ADX payload. New daily reports omit V2 by explicit profile rule. Complete staging reports show
V2 details without a shadow `PASS` or `FAIL` label and without a cross-formula score delta.

For each terminal-proven issue card, V2 treats the existing assessment `root_cause` field as
`diagnosis_correct`. Diagnosis is a gate rather than a weighted field: an incorrect diagnosis gives
that card zero selected-card quality. The remaining native card fields are weighted as follows:
title 5%; description 25%; category 5%; severity 15%; proposed fix 25%; and linked traces 25%.
A `MISMATCHED` card is capped at 40 even when its diagnosis is correct. `NOISE`, `DUPLICATE`, and
`INCOMPLETE` cards cannot be primaries.

One primary is selected per expected issue by highest gated quality, then by ascending stable card
reference to break ties. Reports store the existing public-safe `sha256:` card reference, finding
type, diagnosis result, and selected quality for the chosen primary.

The V2 components are:

```text
N = expected issues
T = issues with an attributable MATCHED, PARTIAL, or MISMATCHED primary
C = 100 * T / N
R = 100 * correct-diagnosis primaries / T
Q = average selected-primary quality among detected issues
U = sum(selected-primary quality) / N = C * Q / 100
G = all generated issue-version cards
B = independently proven baseline noise cards
P = 100 * T / (G + B)
S = 0.80 * U + 0.20 * P
```

When `G + B` is zero, precision displays `N/A` and contributes zero to `S`. Valid, independently
proven baseline Agent findings are score-neutral and excluded from `G`, `B`, and `T`. Comparisons and
gate checks use unrounded values; reports display one decimal place.

Shadow diagnostics record which candidate gates are below target: `S >= 90`, `C >= 95`, `R >= 95`,
`P >= 80`, and `B = 0`. These diagnostics have no automation authority. Incomplete staging evidence
keeps raw V2 counts and the existing incomplete reasons, but V2 total, all V2 components, gate
diagnostics, and selected-primary details are `null`.

The candidate must run unchanged in shadow for three complete staging qualifications. A policy switch
requires separate human review of those calibration results and an explicit change to official
scoring, status, promotion, publication, and historical-comparison contracts.

## FAIL

A complete, trustworthy run is `FAIL` when its quality score is below `90/100`.

## INCOMPLETE

A run is `INCOMPLETE` when identity, quota, deployment, endpoint traffic, a baseline semantic or
terminal proof, a designated issue activation assertion, trace ingestion, Agent Insights execution,
exact-version attribution, assessment, source integrity, or report consistency prevents a trusted
complete result.

Any baseline assessment with an `inconclusive` verdict or issue assessment with an `INCOMPLETE`
finding makes the entire run `INCOMPLETE`. The final quality score is `null`; partial field metrics
must not be presented as a trusted product-quality score.

Efficiency metrics are evidence, not bonus points. `PASS` and `FAIL` are determined only by the
reviewed quality-score formula and threshold.
