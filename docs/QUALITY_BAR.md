# Quality Score

## Quality score

Every complete Daily or staging run produces one score from `0` to `100`:

```text
score = 100 * correct issues / (expected issues + noise cards + duplicate cards)
```

The score is rounded to one decimal place. Whole-number scores render without a decimal.

An expected issue is **Correct** when at least one attributable Insight passes all four scoring
fields:

- title
- description
- category
- linked traces

Severity and proposed fix remain assessed and visible for diagnosis, but they do not affect the
score. There are no field weights or partial points.

Linked traces pass when at least one exact-run, exact-version trace independently supports the
Insight's core conclusion. Extra linked traces are accepted unless they are attributed to the wrong
run or version, or contradict the conclusion.

## Result categories

- **Correct**: an attributable Insight passes all four scoring fields.
- **Incorrect**: an attributable Insight exists, but none passes all four scoring fields.
- **Missing**: no attributable Insight covers the expected issue.
- **Noise**: an extra false-positive Insight is unrelated to every expected issue.
- **Duplicate**: an extra Insight repeats an attributable Insight for the same expected issue.

`correct + incorrect + missing = expected`. Incorrect and Missing issues already occupy an expected
issue slot, so they do not expand the denominator. Noise and Duplicate cards are extra output and do
expand it.

For example:

```text
20 expected = 17 correct + 2 incorrect + 1 missing
1 noise + 1 duplicate
score = 100 * 17 / (20 + 1 + 1) = 77.3
```

Daily selects 20 issues, four per Agent, but its score denominator contains only issues whose runtime
pipeline reached assessment. Explicit `skipped_telemetry`, `skipped_agent_activation`, and
`skipped_insight` issues are excluded from numerator and denominator and remain visible with exact
reasons. Noise and Duplicate penalties count only eligible assessed issue cards. If no issue remains
eligible, no numeric score is produced. Full staging uses all 36 issues. Baseline cards remain
diagnostic ownership evidence and do not change the issue score.

The report shows only the numeric score, its same-formula delta, and the raw category counts. There
is no PASS/FAIL label, threshold, or automated public-preview decision. Humans use the score and
supporting evidence to decide readiness.

## Complete evidence only

A score is published only when every eligible issue has complete identity, endpoint, trace, Agent
Insights, attribution, assessment, source-integrity, and report evidence. Every exclusion and missing
baseline coverage must be explicit; no report may imply 20-issue coverage when fewer issues were
eligible.

If any required evidence is incomplete, the qualification fails internally. Private durable
diagnostics are retained, but no report, email request, ADX row, trend point, generated pull request,
or promotion receipt is produced.

The living Insight Engine improvement memory remains advisory and score-neutral. It can synthesize
only `insight_engine`-owned findings and never changes per-card assessment, ownership, score, or
promotion.

See [Insight Result Labels](INSIGHT_RESULTS.md) for detailed field examples.
