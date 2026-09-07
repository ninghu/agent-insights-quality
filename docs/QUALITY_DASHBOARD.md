# Quality dashboard

The repository template is `dashboards/agent-insights-quality.template.json`.
It is a **read-only view of published, public-safe results**, not a second scorer,
an evidence browser, or a deployment. Private TEST runs, including acceptance
trials, must never be published to ADX to populate it.

## Three pages

| Page | What to read |
| --- | --- |
| **Overview** | Latest stored score and C/E, M, N, D, exclusions; score trend; current/previous count and weighted-penalty changes; scored/planned issue and baseline coverage with comparison limits. |
| **Explain change** | Choose a snapshot (All means latest) and optionally an Agent. The top row identifies both dates and comparison limits; expand `Metadata` for source/run aliases and recorded policy. Agent counts, paired unit outcomes and grouped scored/unscored findings explain the gaps. |
| **Legacy history (read-only)** | Historical stored scores, daily counts and issue outcomes, with legacy-only date/Agent filters. Historical meanings are not rewritten as current-policy results. |

Overview and Explain change use current `AIQRunsV1`, `AIQUnitsV1` and
`AIQFindingsV1` contracts through the dashboard read models. Their region and
scoring-policy selectors are static, reviewed choices that default to Sweden
Central and v2; they do not need published rows or a successful query to be valid.
The default range is **last 14 days**. A selected policy with
no publications yields no score, not a fallback to another policy or to legacy
data. Overview and Explain change explicitly explain an empty official dataset;
no zero score or placeholder traffic is manufactured. The Agent selector includes previous-only Agents so rotated-out units
remain inspectable. It does not change the global score or comparison.

No overall PASS/FAIL threshold, healthy-baseline score bonus, per-Agent quality
score, Full/Partial badge, raw card prose, HTML, or model/provider/recipient URLs
is added. Operational events and assessor configuration are not score-series
dimensions. Assessor identity is not present in this public contract and is not
invented by the dashboard.

## Reading a drop

1. Check **coverage and comparison limits** before interpreting the difference.
   `E` counts scored expected issues; `C` is detected issues; `M = E - C`.
   An excluded unit contributes no counts or misses. Baselines contribute scored
   Noise/Duplicate penalties, never an expected issue or a healthy bonus.
2. Compare C/M/N/D counts and their changes. Weighted values are **denominator
   terms**, not percentage-point attribution or a counterfactual adjusted score.
3. On Explain change, compare the same unit's current and previous status/counts.
   Blank counts or deltas mean excluded, not planned, no prior snapshot, or
   inapplicable baseline C/E/M—not zero. Rotated-out units remain visible.
4. Keep unscored findings separate. An unexpected real finding is not Noise.
   Same-ID card updates/copies have already been reconciled by the result model;
   distinct Duplicate cards remain distinct.

`QualityScore` is read directly from the unified result's stored, one-decimal
score. It is never recomputed by a tile or function:

| Recorded policy | Historical/current formula | Weighted columns |
| --- | --- | --- |
| `unique-issues-noise-1-duplicate-025-v1` | `100*C/(E_scored+N_scored+0.25*D_scored)` | N × 1, D × 0.25; separate miss weight/term stays null, because M is already part of E. |
| `unique-issues-noise-1-duplicate-05-miss-025-v2` | `100*C/(C+N_scored+0.5*D_scored+0.25*(E_scored-C))` | N × 1, D × 0.5, M × 0.25. |

Weight displays require the recorded version, formula, weights and rounding to
match a reviewed policy. Unrecognized combinations get null weighted values and
a comparison warning, not guessed weights. `MissWeight` is projected from the
existing dynamic payload; absent v1 values stay null. No table-column migration,
publication DTO change or privacy-allowlist expansion is required.

## Exact snapshot and comparison rules

`infra/quality-analytics.kql` defines the following read models:

- **`AIQReportsV1()`** collapses exact replay by framework run alias and keeps
  **first ingestion**, not last replay time. Multiple content hashes for one
  alias exclude that run from the score views. `AIQPublicationConflictsV1()`
  remains available for operator investigation.
- **`AIQSnapshotRunsV1(startDate, endDate, regionKey, scoringPolicy)`** is the
  lightweight metadata selector. It chooses one row per report date, normalized region,
  scoring-policy version and coverage-policy version. Of distinct same-date
  runs, latest **first-ingestion time** wins; a timestamp tie uses the lexical
  framework run alias. This is publication ordering, not inferred execution
  order or numeric parsing of a rerun suffix. Re-ingesting an older run cannot
  supersede a newer snapshot. A genuinely new late publication can revise that
  date's selected snapshot. Missing ingestion times are explicitly limited.
- **`AIQSnapshotsV1(startDate, endDate, regionKey, scoringPolicy)`** adds unit
  cohort/coverage checks only for selected run aliases, not every historical unit.
- **`AIQSnapshotChangesV1(startDate, endDate, regionKey, scoringPolicy)`** selects the **immediately earlier report-date
  snapshot in that same region/scoring/coverage-policy series**. It does this
  before the dashboard time filter, so the predecessor can fall outside the
  visible range. It never compares same-date revisions, crosses policy/region,
  or skips an inconvenient cohort to find a more favorable comparator.
- **`AIQUnitPairV1(runAlias, previousRunAlias)`** pairs the selected snapshots with a full outer
  join on Agent, logical version, kind and expected issue alias. Removed and
  newly planned units are not imputed as misses. `AIQUnitChangesV1(runAlias)`
  remains a compatibility wrapper for older callers.

Cohorts compare sorted complete identity sets, not just counts. Changed planned
cohort/rotation, changed scored-unit identities, changed baseline coverage,
incomplete unit details, unknown policy or no predecessor produce a visible
`Limited:` explanation. Equal exclusion counts do not prove equal coverage.
Rollups check public run totals against scored-unit details, including baseline
penalties. The trend keeps separate coverage-policy series, not noisy
Full/Partial/coverage-count legend variants.

Even unchanged recorded cohorts/coverage do **not** establish an Engine-caused
change. Source, assessor, evidence availability and real execution differences
still require review of private evidence. Source commits are available in the
metadata drilldown; the public rows cannot prove identical deployment inputs.
Missing publications are not fabricated as zero-score days. For an unexpectedly
stale or absent snapshot, check publication delivery and the conflict function;
absence of a dashboard row is not a successful run.

## Bounded reads, not a shortened comparison history

The filters, latest-score tile and trend use run metadata rather than unit
rollups. Region and policy choices make **zero ADX requests**. Snapshot selectors
read only the requested date/region/policy metadata. The Agent selector expands
only the current and previous run's units; it never loads all historic Agents.

For comparisons, metadata may still be searched **before the start date** to
find the exact predecessor. `AIQSnapshotRunsV1(..., true)` retains at most one
pre-window snapshot **per visible region/scoring/coverage-policy series** in
addition to the complete visible range. It does not retain seeds for a series
with no visible results. Cohort expansion then reads only those run aliases.
The seed is removed from `AIQSnapshotChangesV1` output only after `prev()` and
all comparison checks have completed. This is not a fixed lookback approximation
or a top-N truncation of the report history.

`AIQReportsV1` first finds candidate aliases using date/region/policy/run-ID
filters, then reconciles **all copies of those aliases** before returning their
payloads. A conflicting copy outside a filter cannot silently become a valid
report. `AIQUnitsV1(runIds)` and `AIQFindingsV1(runIds)` scope report identities
before expanding arrays. Empty selections pass `dynamic([])`, not the
`dynamic(null)` compatibility default that means unrestricted access.

Within a query, metadata and paired unit inputs are materialized when reused.
Separate dashboard tiles remain separate queries; no cross-tile cache or latency
improvement is claimed. Metadata-only tiles avoid cohort joins, and whole-range
comparison tiles expand the visible snapshots plus their required predecessors.
Older zero-argument function calls remain supported but are not the new
template's performance path.

## Import and maintenance

This source change does **not** update any live dashboard, run ADX commands, or
change Azure resources. After review, an authorized operator must:

1. **Install functions before importing the template.** Check that the intended
   database has the current public report table and required functions:

   ```kusto
   .show tables
   | where TableName == 'QualityReportsV1'
   | project TableName
   ```

   ```kusto
   .show functions
   | where Name in ('AIQReportsV1', 'AIQRunsV1', 'AIQUnitsV1', 'AIQFindingsV1',
                    'AIQSnapshotRunsV1', 'AIQSnapshotsV1', 'AIQSnapshotChangesV1',
                    'AIQUnitPairV1', 'AIQUnitChangesV1')
   | project Name, Parameters
   ```

   Apply the reviewed `.create-or-alter function` definitions from
   `infra/quality-analytics.kql` in this dependency order:
   **AIQReportsV1 → AIQRunsV1 → AIQUnitsV1 → AIQFindingsV1 →
   AIQSnapshotRunsV1 → AIQSnapshotsV1 → AIQSnapshotChangesV1 →
   AIQUnitPairV1 → AIQUnitChangesV1**.
   Update existing definitions/signatures too; presence of a function name alone
   is insufficient. Installing only the dashboard JSON does not install KQL.
   If the table is absent, stop and arrange the separately reviewed analytics
   setup rather than creating or seeding data from a private TEST run.

   Existing `.create-merge` table definitions remain compatible; no historical
   data or functions are deleted. The infrastructure script's existing
   `forceUpdateTag` is unchanged, so a redeploy alone must not be assumed to have
   refreshed these functions.
2. **Smoke-check the functions before import**, including a valid empty dataset.
   These read-only checks return aggregate counts and must execute successfully;
   zero is a valid result. A function/table/access error is not an empty dataset.

   ```kusto
   AIQSnapshotRunsV1(ago(14d), now(), 'swedencentral',
       'unique-issues-noise-1-duplicate-05-miss-025-v2')
   | summarize Snapshots = count()
   ```

   ```kusto
   AIQSnapshotChangesV1(ago(14d), now(), 'swedencentral',
       'unique-issues-noise-1-duplicate-05-miss-025-v2')
   | summarize Snapshots = count(), LimitedComparisons = countif(ComparisonLimited)
   ```

   ```kusto
   let Selected = materialize(AIQSnapshotRunsV1(ago(14d), now(), 'swedencentral',
       'unique-issues-noise-1-duplicate-05-miss-025-v2', true)
       | where ReportDate >= startofday(ago(14d))
       | sort by ReportDate desc, SnapshotOrder desc
       | take 1);
   AIQUnitPairV1(toscalar(Selected | project FrameworkRunId),
       toscalar(Selected | project PreviousRunId))
   | summarize PairedUnits = count()
   ```

3. Render the template with the approved `ADX_CLUSTER_URI`, `ADX_DATABASE` and
   `ADX_CLUSTER_NAME` placeholders using private configuration, then import it.
   Never commit the rendered resource locations or IDs.
4. Verify selected-policy empty states, filters, layout, prior-date selection,
   replay/revision behavior and numeric/null rendering against authorized
   **official public-safe** publications. Do not publish private TEST data for
   this check. No automatic refresh/deployment was enabled by this change.

For red filter marks or tiles stuck loading, inspect the query error in the
intended database and the function/schema checks above. Static region/policy
choices fix invalid no-data defaults; they do **not** hide missing functions,
authorization failures, or a wrong data source. Missing-function failures must
be repaired by the operator's reviewed function installation, not swallowed by
an empty-data fallback. Valid empty datasets explain that only official
public-safe publications populate this view; private TEST N7/N8 must stay private.

Legacy `DailyQualityPublications`, all `AIQDaily*` functions, old reports and
legacy West US resources remain untouched. The compact legacy page is not a
migration; the original card/baseline/field/highlight functions remain queryable
for deeper historical maintenance. `AIQOperationsV1()` remains a separate
operational view rather than a wall of score-dashboard tiles.

Offline checks live in `tests/unit/test_dashboard.py` and
`tests/unit/test_publication.py`: JSON references/layout/filter scope, query
contracts and synthetic v1/v2, exclusion, rotation, replay and revision examples.
The snapshot examples are an explicit reference specification, **not KQL
execution**. Public dashboard-schema validation can check JSON compatibility,
but neither it nor pytest proves deployed KQL execution or ADX rendering.
