# Quality dashboard

The repository template is `dashboards/agent-insights-quality.template.json`.
It is a **read-only view of published, public-safe results**, not a second scorer,
an evidence browser, or a deployment. Private TEST runs, including acceptance
trials, must never be published to ADX to populate it.

## Two pages, fixed Sweden Central / v2 scope

| Page | What to read |
| --- | --- |
| **Overview** | Latest stored global score and C/E, M, N, D, exclusions; **Scores by category directly below the headline**, including the separate Baseline penalties row and category comparison limits; then the global trend, count/penalty changes and coverage. |
| **Explain change** | Choose a snapshot (All means latest), optionally a test category and Agent. The unfiltered top row identifies both dates, global scores and comparison limits; expand `CategoryContext` for category availability/limits and `Metadata` for source/run aliases and recorded policy. Filtered Agent counts, paired units and current/prior scored/unscored findings explain the gaps. |

Overview and Explain change use current `AIQRunsV1`, `AIQUnitsV1` and
`AIQFindingsV1` contracts through bounded dashboard read models. All nine tiles
and three query-backed filters explicitly bind **Sweden Central**
(`swedencentral`) and **`unique-issues-noise-1-duplicate-05-miss-025-v2`**.
There are no region, policy or legacy-history selectors/pages.

**Report dates** defaults to **last 14 days**. **Snapshot**, **Test category**
and **Agent** appear only on Explain change, where they are consumed. Their
default `All` selection remains valid without published data. The category and
Agent selectors include previous-only membership, keeping rotated-out units
inspectable without changing the global score, trend or comparison.

No official v2 publication means no score, not a fallback to another
policy/region or legacy data. Both pages explain an empty dataset with null
scores/counts rather than zeros. Private TEST results must never be published
to populate these views.

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

| Displayed policy | Formula | Weighted columns |
| --- | --- | --- |
| `unique-issues-noise-1-duplicate-05-miss-025-v2` | `100*C/(C+N_scored+0.5*D_scored+0.25*(E_scored-C))` | N × 1, D × 0.5, M × 0.25. |

Weight displays require the recorded version, formula, weights and rounding to
match a reviewed policy. Unrecognized combinations get null weighted values and
a comparison warning, not guessed weights. `MissWeight` is projected from the
existing dynamic payload. Historical backend v1 support and absent v1 miss
weights remain unchanged; this dashboard does not select those rows. No
table-column migration is required for these dynamic-payload projections.

## Test-category scores and drilldown

The new optional public payload `category_breakdown` records version
`catalog-test-category-v1`, all eight categories in alphabetical order, and a
separate `baseline` counts/coverage bucket:

`context_memory`, `cost_tokens`, `hallucinations`, `latency`, `output_quality`,
`reliability_errors`, `safety_guardrails`, `tool_call_failures`.

These measure the **frozen catalog category of the issue being tested**, not
the category an Insights card reports. Every whole issue unit's C/E/N/D belongs
to that test category, even Noise that mentions another category. Excluded
issue units retain their category and planned coverage but contribute zero
counts, including misses. A category with no scored expected issues displays
**N/A**, whether it has no planned issues or only excluded issues. This is not
zero or 100.

Python computes and stores each category score once using the run's recorded
policy and rounding. KQL only projects it; it never scores, rescores, averages
categories, or redistributes penalties. The **overall score is not a category
average**. The Overview table shows each stored score, C/E, Miss, Noise,
Duplicate, scored/planned issues, exclusions, prior stored score and comparison
limits. Stored numeric values are unchanged; the table renders null category
scores as `N/A` and unavailable metadata as `Unavailable`.

**Baseline is not a ninth test category.** Its separate row shows only stored
global baseline penalties, scored/planned baselines and exclusions. Baseline
C/E/M and score are inapplicable and displayed blank/`—`; its contract C/E is
zero. No healthy-baseline score or bonus is invented, and no baseline penalty
is split among categories.

To investigate a category, switch to **Explain change** and use the
**Test category** selector, then optionally an Agent. This is an explicit
selector, **not click-to-filter on the Overview table**. `Baseline — global
penalties only` isolates baseline units. Table links remain disabled.

- Agent counts and grouped findings use each snapshot's actual unit category;
  findings show Current/Previous and keep scored/unscored rows separate.
- Paired units match the chosen category on **either side**, so a reassigned or
  rotated-out issue remains visible. `PreviousCategory` and `CurrentCategory`
  disclose moves. Their deltas compare that same unit, not an in-category
  attribution; the opposite side may belong to another category.
- The global context above these tables remains unfiltered. No filtered
  per-Agent score is calculated.
- `All` includes old issue rows whose category is unavailable. A specific
  category cannot classify those rows retrospectively. Empty filtered tables
  mean no matching recorded units/findings, not a zero score or zero counts;
  reset Agent/category filters when inspecting a different snapshot.

Older publications without `category_breakdown` retain their global score but
show unavailable category rows, not invented history. No lookup of today's
catalog, previous categorized publication or legacy `AIQDaily*` rows fills the
gap. With no matching publication at all, the headline/context explains the
empty dataset and the category table has no rows. A missing
or unsupported category contract, or counts/coverage that do not match the
validated unit details, is unavailable rather than scorable.

## Exact snapshot and comparison rules

`infra/quality-analytics.kql` defines the following read models:

- **`AIQReportsV1(...)`** collapses exact replay by framework run alias and keeps
  **first ingestion**, not last replay time. Multiple content hashes for one
  alias exclude that run from the score views. `AIQPublicationConflictsV1()`
  remains available for operator investigation.
- **`AIQSnapshotRunsV1(startDate, endDate, regionKey, scoringPolicy, includePrevious)`**
  is the lightweight metadata selector. It chooses one row per report date, normalized region,
  scoring-policy version and coverage-policy version. Of distinct same-date
  runs, latest **first-ingestion time** wins; a timestamp tie uses the lexical
  framework run alias. This is publication ordering, not inferred execution
  order or numeric parsing of a rerun suffix. Re-ingesting an older run cannot
  supersede a newer snapshot. A genuinely new late publication can revise that
  date's selected snapshot. Missing ingestion times are explicitly limited.
- **`AIQSnapshotsV1(..., includePrevious, runIds)`** validates cohort/coverage
  details only for selected run aliases, not every historical unit.
- **`AIQSnapshotChangesV1(..., selectedRunIds)`** compares the **immediately earlier report-date
  snapshot in that same region/scoring/coverage-policy series**. It does this
  before the dashboard time filter, so the predecessor can fall outside the
  visible range. It never compares same-date revisions, crosses policy/region,
  or skips an inconvenient cohort to find a more favorable comparator.
- **`AIQUnitPairV1(runAlias, previousRunAlias)`** pairs the selected snapshots with a full outer
  join on Agent, logical version, kind and expected issue alias. Removed and
  newly planned units are not imputed as misses. Current/previous test categories
  are carried separately, not added to the pairing key. `AIQUnitChangesV1`
  remains a compatibility wrapper, not a template query path.
- **`AIQCategorySnapshotsV1(..., runIds)`** joins those selected snapshots to the immutable
  public reports and category-grouped unit details. It projects the stored
  category/baseline counts and coverage, checks their agreement with units, and
  emits all eight category rows plus Baseline even when metadata is unavailable.
  It does not select a different snapshot to obtain category metadata.
- **`AIQCategoryChangesV1(..., selectedRunIds)`** joins by the existing exact `PreviousRunId` and
  category. It inherits global comparison limits and checks complete sorted
  planned/scored membership within each bucket. Rotation, category reassignment,
  changed scored membership and source commits are disclosed. No prior
  snapshot, an unavailable current/prior category contract, or a category with
  no scored expected issues remains an explicit limit. Even equal category
  coverage counts can conceal changed identities.

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
Category comparison notes repeat this limitation: even the same source and
recorded membership do not prove the same assessor, evidence or execution.
An immediate prior date with old metadata stays unavailable even if an older
categorized date exists. Similarly, a later same-date revision without category
metadata wins under the ordinary snapshot rules; earlier categorized revisions
are not substituted.
Missing publications are not fabricated as zero-score days. For an unexpectedly
stale or absent snapshot, check publication delivery and the conflict function;
absence of a dashboard row is not a successful run.

## Bounded reads without shortened comparison history

The headline, trend and Snapshot selector use lightweight run metadata, not
unit rollups. Category/Agent selectors, Agent counts, findings and unit pairs
expand only the selected current/prior runs. Category scores, the latest count
comparison and Explain change context also pass the exact selected run-ID set
into their read models **before** cohort expansion. The range coverage table
expands the visible snapshots and their required predecessors.

`AIQSnapshotRunsV1(..., true)` may search metadata before the start date to find
the exact predecessor. It retains at most one pre-window snapshot **per visible
region/scoring/coverage-policy series**, with no seed for a series that has no
visible rows. This is not a fixed lookback or top-N approximation. Comparisons
retain the seed until after `prev()` and cohort checks; only requested current
rows are returned. Selected-run filtering never substitutes a different
predecessor, including one with more convenient category metadata.

`AIQReportsV1` finds candidate aliases by date/region/policy/run IDs, then
reconciles **every copy** of each alias. A conflicting copy outside a filter
still invalidates the identity; replays still retain first ingestion.
`AIQUnitsV1(runIds)` and `AIQFindingsV1(runIds)` scope reports before expanding
arrays. `AIQCategorySnapshotsV1` scopes both report and unit reads to the same
bounded run set. Stored scores are never recomputed.

An empty selection passes `dynamic([])`, never the `dynamic(null)` compatibility
default meaning unrestricted access. Selecting a missing/stale snapshot must
not select the latest run instead. Existing no-argument backend callers remain
supported but are not used by this template. Reused inputs are materialized
within queries; separate tiles remain separate queries. No cross-tile cache,
measured latency improvement or bounded metadata-history search is claimed.

## Import and maintenance

This source change does **not** update any live dashboard, run ADX commands, or
change Azure resources. After review, an authorized operator must:

1. **Install the updated functions before importing the template.** Confirm
   `QualityReportsV1` exists in the intended database. If absent, stop and
   arrange the separately reviewed analytics setup, not private TEST seeding.
   Check definitions/signatures, not merely function names:

   ```kusto
   .show functions
   | where Name in ('AIQReportsV1', 'AIQRunsV1', 'AIQUnitsV1', 'AIQFindingsV1',
                    'AIQSnapshotRunsV1', 'AIQSnapshotsV1', 'AIQSnapshotChangesV1',
                    'AIQCategorySnapshotsV1', 'AIQCategoryChangesV1',
                    'AIQUnitPairV1', 'AIQUnitChangesV1')
   | project Name, Parameters
   ```

   Apply the `.create-or-alter function` definitions in this dependency order:
   **AIQReportsV1 → AIQRunsV1 → AIQUnitsV1 → AIQFindingsV1 →
   AIQSnapshotRunsV1 → AIQSnapshotsV1 → AIQSnapshotChangesV1 →
   AIQCategorySnapshotsV1 → AIQCategoryChangesV1 → AIQUnitPairV1 →
   AIQUnitChangesV1**.

   In particular, reports/runs accept optional date/region/policy/run-ID bounds;
   units/findings accept `runIds`; snapshots accept `includePrevious` and
   `runIds`; snapshot/category changes accept `selectedRunIds`; category
   snapshots accept `runIds`. Older zero-argument category definitions alone
   cannot serve the new template. Installing JSON does not install these KQL
   signatures, and this integration is **not JSON-only**.

   Existing `.create-merge` table definitions remain compatible; no historical
   data or functions are deleted. The infrastructure script's existing
   `forceUpdateTag` is unchanged, so a redeploy alone must not be assumed to have
   refreshed these functions.
2. **Smoke-check the functions before import.** For example, these read-only
   queries should succeed with zero counts on a valid empty dataset:

   ```kusto
   AIQSnapshotRunsV1(ago(14d), now(), 'swedencentral',
       'unique-issues-noise-1-duplicate-05-miss-025-v2')
   | summarize Snapshots = count()
   ```

   ```kusto
   let Selected = AIQSnapshotRunsV1(ago(14d), now(), 'swedencentral',
       'unique-issues-noise-1-duplicate-05-miss-025-v2')
       | sort by ReportDate desc, SnapshotOrder desc | take 1;
   let SelectedRunIds = toscalar(Selected | summarize make_set(FrameworkRunId));
   AIQCategoryChangesV1(ago(14d), now(), 'swedencentral',
       'unique-issues-noise-1-duplicate-05-miss-025-v2', SelectedRunIds)
   | summarize Rows = count(), Available = countif(CategoryAvailable)
   ```

   A missing function/table, authorization error or wrong data source is **not**
   an empty dataset. Do not swallow such errors with fuzzy/best-effort fallbacks.
3. Render the template with the existing `ADX_CLUSTER_URI`, `ADX_DATABASE` and
   `ADX_CLUSTER_NAME` placeholders using private configuration, then update the
   existing authorized dashboard. Never commit rendered resource locations/IDs.
   Old saved URLs may retain removed page/filter parameters: use the updated
   Overview entry point without obsolete region, policy or legacy filters.
4. Verify fixed Sweden/v2 and old-category empty states, category/Baseline
   selectors, reassigned/previous-only units, layout, prior-date selection,
   replay/revision behavior and numeric/null rendering against authorized
   **official public-safe** publications. Do not publish private TEST data for
   this check. No automatic refresh/deployment was enabled by this change.

Legacy history is removed **only from the UI**. `DailyQualityPublications`,
all `AIQDaily*` functions, old reports, legacy West US resources and backend v1
support remain untouched. Their original meanings are not rewritten or inferred.
`AIQOperationsV1()` remains a separate operational view; this UI integration
does not remove latency measurements or change Daily email fields.

Offline checks live in `tests/unit/test_dashboard.py` and
`tests/unit/test_publication.py`: JSON references/layout/filter scope, query
contracts and model-generated v1/v2 category scores, whole-unit exclusions,
baseline penalties, category reassignment, unavailable history, rotation,
replay/revision, cross-filter conflicts, bounded exact predecessors and empty
run-selection examples.
The snapshot examples are an explicit reference specification, **not KQL
execution**. Public dashboard-schema validation can check JSON compatibility,
but neither it nor pytest proves deployed KQL execution or ADX rendering.
