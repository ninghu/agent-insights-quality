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

The report stores both component scores and the final score to one decimal place when needed.
Structural, privacy, provenance, and
reporting evidence must still be complete and trustworthy before a score can be used.

## FAIL

A complete, trustworthy run is `FAIL` when its quality score is below `90/100`.

## INCOMPLETE

A run is `INCOMPLETE` when identity, quota, deployment, endpoint traffic, trace ingestion, Agent
Insights execution, exact-version attribution, assessment, or report consistency prevents a trusted
complete result.

Efficiency metrics are evidence, not bonus points. `PASS` and `FAIL` are determined only by the
reviewed quality-score formula and threshold.
