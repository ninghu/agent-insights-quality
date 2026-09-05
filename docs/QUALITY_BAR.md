# Evidence and quality rules

## Staging

Staging normally selects changed, missing or incomplete targets. First use or an explicit full run
covers five baselines and 36 issues with ten attempts per target. There is no deployed paired-v0.

Baselines require eight adequately evidenced healthy attempts with no proven healthy-contract
violation. Deterministic issues require eight proven defect observations with no proven contradiction
of the deterministic contract. Probability-tolerant issues also require eight observations out of ten.
Evaluate all ten attempts, not just the first eight successes.

Missing evidence is not the same as a behavior failure. Preserve PASS, FAIL and INCOMPLETE
separately, and never resample a behavioral miss until it passes.

For example, seven observations plus three sufficiently evidenced nonobservations fail the
observation threshold. Six observations plus four insufficient attempts are INCOMPLETE, not a
proven behavioral failure. Eight healthy observations cannot hide a proven baseline violation.
Record the policy with each assessment. Historical six-of-ten judgments remain historical;
apply the new policy to retained valid evidence without overwriting the original result.

## Daily readiness and assessment

Daily plans 20 issues and five baselines. Six distinct attributable probe attempts out of ten
establish telemetry readiness, not defect correctness. It does not require every child span or
repeat staging's deep behavioral checks before Insights.
The eight-observation staging policy does not raise this six-attempt Daily readiness requirement.

Core diagnosis, reasonable category and independently supporting current evidence determine
correctness. Severity and suggested fixes are diagnostic only. A candidate gap receives bounded
review using already-generated evidence, not new Agent traffic.

- **Detected:** a correct card identifies the expected defect; each expected issue counts once.
- **Noise:** an in-scope card's core judgment is demonstrably incorrect, including wrong diagnosis,
  material category error or wrong evidence.
- **Duplicate:** an otherwise correct extra distinct card repeats the same causal problem.
- **Unexpected real finding:** a supported problem outside the expected issue, including a genuine
  baseline defect; it is not Noise and does not inflate expected-issue detection.
- **Unconfirmed:** evidence cannot establish a reliable core verdict.

The same card is never both Noise and Duplicate. Repeated incorrect cards remain Noise. Same-ID
updates, reopenings and transport duplicates are not additional duplicate cards.

## Score and coverage

```text
score = 100 * C / (E_scored + N_scored + 0.25 * D_scored)
```

`C` is the number of distinct correctly detected scored issues. Noise and Duplicate include
scorable baselines as well as issues; baselines add no healthy bonus. There is no overall
quality PASS/FAIL threshold. A fully measured zero is valid; an unmeasured run is not zero.

Full covers all 25 units. Partial permits at most two unscorable baseline/issue units and at
least one scorable issue. Exclude each entire unit from all score counts, not just unfavorable
cards, and show planned/scored coverage, exclusions and reasons. Confirmed findings in excluded
units remain visible as unscored diagnostics.

More than two unscorable units, no scorable issue or systemic integrity failure produces no team
score/report and only a private failure notice. Compare trends with scoring policy and coverage
visible; Partial is not interchangeable with Full.
