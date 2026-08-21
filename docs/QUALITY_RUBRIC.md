# Quality Rubric

The daily verdict is strict and fail-closed.

`AT BAR` requires a complete full-catalog run, zero healthy-baseline insights, no run with more than
five insights, 100% high-severity recall, at least 90% overall recall, at least 95% precision, and
100% category, severity, title, description, proposed-fix, and linked-trace correctness among
accepted true positives. Duplication, umbrella, and cross-version stale-evidence rates must all be
zero. Structural, provenance, secret/PII, judge-schema, or unresolved-classification failures are
not permitted.

`NOT AT BAR` means a complete, trustworthy run proved at least one quality gate failed.
`INCONCLUSIVE` means identity, infrastructure, quota, trace ingestion, production API, judging,
report consistency, or another prerequisite prevented a trustworthy complete result. An incomplete
run is never represented as a pass.

An insight is a true positive only when it maps to one independently fixable expected root cause and
all required attributes pass. `partially_useful` output remains visible but does not count as a true
positive. Efficiency metrics such as latency, model calls, and tokens are diagnostics, not substitutes
for quality.

Automatic ADO action requires one reproducible complete occurrence, deterministic and provenance
checks, retained evidence, a successful duplicate search, and independent agreement from the primary
and blinded Copilot judgments at confidence `>= 0.95`.
