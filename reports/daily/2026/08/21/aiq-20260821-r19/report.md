# Agent Insights Quality Report - 2026-08-21

- Report: `aiq-20260821-r19`
- Overall insight quality score: **0/100**
- Canonical audit verdict: `NOT AT BAR`
- Engine: `public-agent-insights-daily` / `gpt-5.6-terra`
- Complete: `true`

## Summary

Among 21 distinct observed cards, 0 were fully correct on customer utility and content, 5 were partially useful, and 16 were incorrect or noisy. These utility grades intentionally exclude lifecycle behavior and collection hygiene. Separately, strict quality-bar matching found 0 of 20 expected problems; 20 did not receive a strict match. Agent Insights did not meet the strict daily quality bar; the gaps below require attention.
The assessment covered 25 of 25 planned scenarios. No structural, provenance, privacy, judge-schema, or unresolved trust failure was recorded, but any human-validation items below still require review.
Partially useful cards remain visible as customer-useful diagnostic signal but do not count as strict true positives. 1 matched the expected root cause but failed category or severity correctness.
The overall score measures strict expected-issue success only. Incorrect/noisy insights and exact duplicates are independent guardrail metrics and do not change the 0-100 score.

Data source: canonical report aiq-20260821-r19, generated 2026-08-22T23:16:24Z; 25 immutable scenario results across 14 run/agent evaluations and 5 synthetic test agents.

| Grade | Findings |
| --- | ---: |
| Overall insight quality score | 0/100 |
| Fully correct (content utility) | 0/21 |
| Partially useful (content utility) | 5/21 |
| Incorrect/noisy insights | 16/21 |
| Exact duplicates | 0 |

## What is working

| Capability | Evidence |
| --- | --- |
| Useful diagnostic signal | 5 of 21 observed cards contained useful signal: 0 met the strict quality bar and 5 were partially useful. Evidence covered Parent-child trace correlation control; Partial tool failure ignored; Task evasion or no-op response. |
| Duplicate and version control | Across 21 cards, analysis found 0 duplicate and 0 stale-version relationships. |

## What needs improvement

| Product gap | What happened | Needed behavior |
| --- | --- | --- |
| Expected roots lacked a strict match | Affected test agents: All test agents. 5 of 20 expected roots were true silent misses with no card. 15 expected roots had card output, but 14 had no root-cause-correct match; 1 root had a matching card that still failed other required content fields. Strict recall was 0.0%. | Detect every high-severity problem and at least 90% of all expected problems with the correct root cause. |
| Incorrect and ambiguous findings | Affected test agents: All test agents. Of 21 observed cards, 16 were incorrect/noisy and 5 were only partially useful; strict quality-bar precision was 0.0%. 5 cards came from healthy controls. | Return no card for healthy behavior and ground each finding in the complete trace, request, available tools, and current agent version. |
| Finding count did not match root causes | Affected test agents: All test agents. 7 run/agent results had count mismatches; 20 findings were expected and 21 were observed. | Produce exactly one clearly scoped finding per independently fixable root cause in each run. |
| Finding content was incomplete or inaccurate | Affected test agents: All test agents. Across 5 mapped cards with fully or partially useful content (the scorecard attribute-rate denominator), category accuracy passed 40.0%, severity accuracy passed 0.0%, title pass rate passed 20.0%, description pass rate passed 60.0%, proposed fix pass rate passed 20.0%, linked trace pass rate passed 60.0%, evidence localization rate passed 60.0%, actionability rate passed 20.0%. | Make every title, explanation, severity, category, trace link, and proposed fix specific, correct, localized, meaningful, and actionable. |
| Related findings were not cleanly separated | Affected test agents: aiq-001-weather-20260821-r19-w01, aiq-002-healthcare-20260821-r19-w02. Analysis found 2 fragment relationships and 1 umbrella relationship. | Group evidence by root cause, avoid duplicate or fragmented cards, and scope each finding to the immutable agent version where it reproduces. |

**Follow-up:** 3 bug candidates prepared; no work-item mutation was claimed.

## Daily assessment

- Expected roots: 20; observed physical cards: 21; strict true positives: 0.
- Root-cause-correct cards: 1 of 21; true silent misses: 5.
- Lifecycle/collection: 0 duplicates, 2 fragments, 1 umbrellas, 0 stale-version relationships.

## Quality bar and result

AT BAR requires exact expected-versus-observed cards for every run and agent; at least 90% recall and 95% precision; 100% required-field correctness; zero duplicate, fragment, umbrella, stale-version, and healthy-control cards; and no trust failure.

**Overall insight quality score: 0/100.** Recall was 0.0%, precision was 0.0%, and required-field correctness was 0.0%.

## Per-agent reports

| Agent | Report | Recommended human validation | Assigned to |
| --- | --- | --- | --- |
| aiq-001-weather-20260821-r19-w01 (`aiq-001-weather`) | [View report](agents/aiq-001-weather.md) | Yes | Han |
| aiq-002-healthcare-20260821-r19-w02 (`aiq-002-healthcare`) | [View report](agents/aiq-002-healthcare.md) | Yes | Ilya |
| aiq-003-finance-20260821-r19-w01 (`aiq-003-finance`) | [View report](agents/aiq-003-finance.md) | Yes | Sean |
| aiq-004-travel-20260821-r19-w02 (`aiq-004-travel`) | [View report](agents/aiq-004-travel.md) | Yes | Billy |
| aiq-005-ticket-20260821-r19-w03 (`aiq-005-ticket`) | [View report](agents/aiq-005-ticket.md) | Yes | Han |
