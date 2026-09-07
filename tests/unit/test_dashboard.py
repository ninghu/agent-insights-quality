"""Offline dashboard contracts and synthetic examples, not a KQL execution engine."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

import pytest

from agent_insights_quality.publication import build_public_report
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, ExclusionReason, PlannedUnit, UnitId, UnitResult,
    aggregate_results,
)
from agent_insights_quality.scoring import LEGACY_SCORING_POLICY, SCORING_POLICY


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = json.loads(
    (ROOT / "dashboards" / "agent-insights-quality.template.json").read_text(encoding="utf-8")
)
KQL = (ROOT / "infra" / "quality-analytics.kql").read_text(encoding="utf-8")
FUNCTIONS = dict(re.findall(r"\) (AIQ\w+)\([^\n]*\) \{\n(.*?)\n\}", KQL, re.DOTALL))
VARIABLES = re.compile(r"\b_[a-zA-Z]\w*\b")


def visible_pages(parameter):
    scope = parameter["showOnPages"]
    if scope["kind"] == "all":
        return {page["id"] for page in DASHBOARD["pages"]}
    assert scope["kind"] == "selection"
    return set(scope["pageIds"])


def test_dashboard_references_layout_and_visual_contracts():
    assert [page["name"] for page in DASHBOARD["pages"]] == [
        "Overview", "Explain change", "Legacy history (read-only)",
    ]
    assert DASHBOARD["schema_version"] == "20"
    assert DASHBOARD["autoRefresh"] == {"enabled": False}
    source, = DASHBOARD["dataSources"]
    assert (source["clusterUri"], source["database"], source["name"]) == (
        "{{ADX_CLUSTER_URI}}", "{{ADX_DATABASE}}", "{{ADX_CLUSTER_NAME}}",
    )
    ids = [item["id"] for collection in ("dataSources", "pages", "parameters", "tiles")
           for item in DASHBOARD[collection]]
    assert len(ids) == len(set(ids))
    assert all(str(UUID(identity)) == identity for identity in ids)
    pages = {page["id"] for page in DASHBOARD["pages"]}
    per_page = defaultdict(list)
    for tile in DASHBOARD["tiles"]:
        assert tile["pageId"] in pages and tile["dataSourceId"] == source["id"]
        layout = tile["layout"]
        x, y, width, height = (layout[key] for key in ("x", "y", "width", "height"))
        assert x >= 0 and y >= 0 and width >= 2 and height >= 1 and x + width <= 24
        for left, top, right, bottom in per_page[tile["pageId"]]:
            assert x >= right or x + width <= left or y >= bottom or y + height <= top
        per_page[tile["pageId"]].append((x, y, x + width, y + height))
        assert set(VARIABLES.findall(tile["query"])) == set(tile["usedParamVariables"])
        assert not re.search(r"^\s*\.", tile["query"], re.MULTILINE)
        assert not re.search(r"\btop\s+\d+\s+by[^\n]*,", tile["query"])
        options = tile["visualOptions"]
        assert not options.get("colorRules")
        if tile["visualType"] == "line":
            assert options["yColumns"]["value"] == ["QualityScore"]
            assert options["seriesColumns"]["value"] == ["Series"]
        else:
            assert tile["visualType"] == "table"
            assert options["table__enableRenderLinks"] is False
    assert [len(per_page[page["id"]]) for page in DASHBOARD["pages"]] == [4, 4, 3]


def test_filters_exist_only_on_pages_that_consume_them_and_use_matching_contracts():
    by_variable = {}
    for parameter in DASHBOARD["parameters"]:
        variables = ([parameter["beginVariableName"], parameter["endVariableName"]]
                     if parameter["kind"] == "duration" else [parameter["variableName"]])
        for variable in variables:
            assert variable not in by_variable
            by_variable[variable] = parameter
            consuming = {tile["pageId"] for tile in DASHBOARD["tiles"]
                         if variable in tile["usedParamVariables"]}
            assert consuming == visible_pages(parameter)
    legacy = DASHBOARD["pages"][-1]["id"]
    for parameter in DASHBOARD["parameters"]:
        if parameter["kind"] == "duration":
            continue
        source = parameter["dataSource"]
        if source["kind"] == "static":
            assert parameter["defaultValue"]["value"] in {
                choice["value"] for choice in source["values"]
            }
            continue
        assert set(source["consumedVariables"]) == set(VARIABLES.findall(source["query"]))
        assert source["dataSourceId"] == DASHBOARD["dataSources"][0]["id"]
        for dependency in source["consumedVariables"]:
            assert visible_pages(parameter) <= visible_pages(by_variable[dependency])
        assert ("AIQDaily" in source["query"]) == (visible_pages(parameter) == {legacy})
    assert by_variable["_policy"]["defaultValue"]["value"] == SCORING_POLICY.version
    assert by_variable["_policy"]["selectionType"] == "single"
    assert by_variable["_region"]["selectionType"] == "single"
    for tile in DASHBOARD["tiles"]:
        assert set(tile["usedParamVariables"]) <= by_variable.keys()
        if tile["pageId"] != legacy:
            assert "AIQDaily" not in tile["query"]
            assert "CoverageStatus" not in tile["query"]
            assert "_startTime, _endTime, _region, _policy" in tile["query"]


def test_query_dependencies_are_defined_current_read_models_not_raw_or_legacy_data():
    queries = [tile["query"] for tile in DASHBOARD["tiles"]]
    queries += [parameter["dataSource"]["query"] for parameter in DASHBOARD["parameters"]
                if "query" in parameter.get("dataSource", {})]
    queries += list(FUNCTIONS.values())
    for query in queries:
        assert set(re.findall(r"\b(AIQ\w+)\(", query)) <= FUNCTIONS.keys()
    current = KQL.split("// Current framework data", 1)[1]
    assert "DailyQualityPublications" not in current
    for name in ("AIQSnapshotRunsV1", "AIQSnapshotsV1", "AIQSnapshotChangesV1", "AIQUnitPairV1", "AIQUnitChangesV1"):
        assert "QualityReportsV1" not in FUNCTIONS[name]
        assert "AIQDaily" not in FUNCTIONS[name]
        assert "_startTime" not in FUNCTIONS[name]
    assert ".create-merge table QualityReportsV1" in current
    assert ".drop" not in KQL


def synthetic_result(policy=SCORING_POLICY, *, excluded=None, rotated=False):
    plan = (
        PlannedUnit(UnitId("agent-a", "v0")),
        PlannedUnit(UnitId("agent-a", "issue-001"), "issue-001"),
        PlannedUnit(UnitId("agent-a", "issue-002"), "issue-002"),
        PlannedUnit(UnitId("agent-b", "v0")),
        PlannedUnit(UnitId("agent-b", "issue-004" if rotated else "issue-003"),
                    "issue-004" if rotated else "issue-003"),
    )
    detected = CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001")
    actual = [
        UnitResult(plan[0].unit_id, (CardVerdict("card-0001", CoreVerdict.INCORRECT),)),
        UnitResult(plan[1].unit_id, (
            detected, detected, CardVerdict("card-0002", CoreVerdict.CORRECT, "issue-001"),
        )),
        UnitResult(plan[2].unit_id),
        UnitResult(plan[3].unit_id),
        UnitResult(plan[4].unit_id, (
            CardVerdict("card-0001", CoreVerdict.CORRECT, plan[4].expected_issue_alias),
        )),
    ]
    if excluded is not None:
        actual[excluded] = replace(
            actual[excluded], exclusion_reasons=(ExclusionReason.INCOMPLETE_EVIDENCE,),
        )
    return aggregate_results(plan, actual, scoring_policy=policy), plan


@pytest.mark.parametrize("policy,score,miss_weight", [
    (LEGACY_SCORING_POLICY, 47.1, None), (SCORING_POLICY, 53.3, 0.25),
])
def test_exact_public_v1_v2_shapes_preserve_scores_and_nullable_miss_weight(policy, score, miss_weight):
    result, plan = synthetic_result(policy)
    envelope = build_public_report(
        result, allowed_units=plan, framework_run_id="daily-2026-09-04-r0",
        report_date="2026-09-04", source_commit="a" * 40, region="Sweden Central",
    )
    payload = envelope["report"]
    assert payload["score"] == score
    assert payload["counts"] == {
        "correct_issues": 2, "expected_issues": 3, "noise_cards": 1, "duplicate_cards": 1,
    }
    assert payload["scoring_policy"].get("miss_weight") == miss_weight
    assert ("miss_weight" in payload["scoring_policy"]) is (policy == SCORING_POLICY)
    projections = dict(re.findall(
        r"(\w+) = (?:todouble|tolong|tostring|tobool)\(Payload\.([a-z_.]+)\)", FUNCTIONS["AIQRunsV1"],
    ))
    assert projections["QualityScore"] == "score"
    assert projections["MissWeight"] == "scoring_policy.miss_weight"
    for column, expected in (("QualityScore", score), ("MissWeight", miss_weight)):
        value = payload
        for key in projections[column].split("."):
            value = value.get(key)
        assert value == expected
    policy_check = FUNCTIONS["AIQSnapshotsV1"]
    assert f"ScoringPolicy == '{policy.version}'" in policy_check
    assert f"ScoreFormula == '{policy.formula}'" in policy_check
    assert "WeightedMisses = iff(PolicyValid, MissWeight * MissedIssues, real(null))" in policy_check
    assert "MissedIssues = ExpectedIssues - CorrectIssues" in FUNCTIONS["AIQRunsV1"]


@pytest.mark.parametrize("excluded,expected", [
    (None, (2, 3, 1, 1, 1, 0)),
    (0, (2, 3, 1, 0, 1, 1)),
    (1, (1, 2, 1, 1, 0, 1)),
    (2, (2, 2, 0, 1, 1, 1)),
])
def test_whole_unit_rollup_domain_examples(excluded, expected):
    result, _ = synthetic_result(excluded=excluded)
    payload = result.to_dict()
    units = payload["units"]
    scored = [unit for unit in units if unit["scorable"]]
    issues = [unit for unit in scored if unit["kind"] == "issue"]
    c = sum(unit["counts"]["correct_issues"] for unit in issues)
    e = sum(unit["counts"]["expected_issues"] for unit in issues)
    n = sum(unit["counts"]["noise_cards"] for unit in scored)
    d = sum(unit["counts"]["duplicate_cards"] for unit in scored)
    assert (c, e, e - c, n, d, len(units) - len(scored)) == expected
    assert (c, e, n, d) == tuple(payload["counts"].values())
    if excluded is not None:
        assert units[excluded]["counts"] == dict.fromkeys(payload["counts"], 0)
        assert not any(finding["scored"] for finding in units[excluded]["findings"])
    cohorts = FUNCTIONS["AIQSnapshotsV1"]
    assert "UnitCorrect = sumif(CorrectIssues, Scorable and Kind == 'issue')" in cohorts
    assert "UnitExpected = sumif(ExpectedIssues, Scorable and Kind == 'issue')" in cohorts
    assert "UnitNoise = sumif(NoiseCards, Scorable)" in cohorts
    assert "UnitDuplicates = sumif(DuplicateCards, Scorable)" in cohorts
    assert "UnitRows == PlannedIssues + PlannedBaselines" in cohorts
    assert "UnitCorrect == CorrectIssues and UnitExpected == ExpectedIssues" in cohorts


def row(day, *, revision=0, policy=SCORING_POLICY, region="Sweden Central", excluded=None):
    result, _ = synthetic_result(policy, excluded=excluded)
    payload = result.to_dict()
    return {
        "FrameworkRunId": f"daily-2026-09-{day:02}-r{revision}",
        "ReportDate": f"2026-09-{day:02}",
        "PublishedAt": f"2026-09-{day:02}T12:{revision:02}:00",
        "Region": region, "ScoringPolicy": policy.version,
        "CoveragePolicy": payload["coverage_policy"]["version"],
        "QualityScore": payload["score"], "Payload": payload,
        "ContentHash": hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
    }


def snapshot_spec(rows):
    """Reference examples for the statically checked operators below; no KQL is run."""
    runs = defaultdict(list)
    for item in rows:
        runs[item["FrameworkRunId"]].append(item)
    days = defaultdict(list)
    for copies in runs.values():
        if len({item["ContentHash"] for item in copies}) != 1:
            continue
        first = min(copies, key=lambda item: item["PublishedAt"])
        key = (
            first["Region"].replace(" ", "").lower(), first["ScoringPolicy"],
            first["CoveragePolicy"], first["ReportDate"],
        )
        days[key].append(first)
    return {
        key: max(candidates, key=lambda item: (item["PublishedAt"], item["FrameworkRunId"]))
        for key, candidates in days.items()
    }


def test_snapshot_selection_deduplicates_replays_and_same_date_revisions():
    old, revision, current = row(1), row(1, revision=1), row(2)
    replay = dict(old, PublishedAt="2026-09-03T12:00:00")
    selected = snapshot_spec([old, current, revision, replay, deepcopy(current)])
    assert [selected[key]["FrameworkRunId"] for key in sorted(selected)] == [
        revision["FrameworkRunId"], current["FrameworkRunId"],
    ]
    conflict = dict(current, ContentHash="b" * 64)
    assert len(snapshot_spec([old, current, conflict])) == 1
    report = FUNCTIONS["AIQReportsV1"]
    assert "arg_min(PublishedAt, *) by FrameworkRunId" in report
    assert "ContentHashes = make_set(ContentHash, 2)" in report
    assert "array_length(ContentHashes) == 1" in report
    snapshot = FUNCTIONS["AIQSnapshotRunsV1"]
    assert "SnapshotOrder = strcat(format_datetime(PublishedAt, 'yyyyMMddHHmmssfffffff'), '/', FrameworkRunId)" in snapshot
    assert "arg_max(SnapshotOrder, *) by ReportDate, RegionKey, ScoringPolicy, CoveragePolicy" in snapshot


def test_previous_snapshot_never_crosses_region_or_policy_and_precedes_date_filter():
    previous, current = row(1), row(4, region="swedencentral")
    legacy = row(3, revision=1, policy=LEGACY_SCORING_POLICY)
    other_region = row(3, revision=2, region="synthetic-other-region")
    selected = snapshot_spec([previous, current, legacy, other_region])
    series = defaultdict(list)
    for key in sorted(selected):
        series[key[:-1]].append(selected[key])
    pair = series[("swedencentral", SCORING_POLICY.version, current["CoveragePolicy"])]
    assert [item["FrameworkRunId"] for item in pair] == [
        previous["FrameworkRunId"], current["FrameworkRunId"],
    ]
    assert previous["ReportDate"] < "2026-09-04"
    changes = FUNCTIONS["AIQSnapshotChangesV1"]
    assert "sort by RegionKey asc, ScoringPolicy asc, CoveragePolicy asc, ReportDate asc" in changes
    assert "| serialize" in changes
    assert "RegionKey == prev(RegionKey) and ScoringPolicy == prev(ScoringPolicy)" in changes
    assert "CoveragePolicy == prev(CoveragePolicy)" in changes
    assert "PreviousRunId = iff(SameSeries, prev(FrameworkRunId), '')" in changes
    assert changes.index("| where isnull(startDate)") > changes.index("PreviousRunId = iff")


def cohort_spec(payload, predicate=lambda unit: True):
    return {
        (unit["unit_id"]["agent"], unit["unit_id"]["logical_version"],
         unit["kind"], unit["expected_issue_alias"])
        for unit in payload["units"] if predicate(unit)
    }


def test_comparison_limits_distinguish_rotation_exclusion_identity_and_baselines():
    complete = synthetic_result()[0].to_dict()
    rotated = synthetic_result(rotated=True)[0].to_dict()
    excluded_a = synthetic_result(excluded=1)[0].to_dict()
    excluded_b = synthetic_result(excluded=2)[0].to_dict()
    baseline_gap = synthetic_result(excluded=0)[0].to_dict()
    assert complete["coverage"] == rotated["coverage"]
    assert cohort_spec(complete) != cohort_spec(rotated)
    assert excluded_a["coverage"] == excluded_b["coverage"]
    assert cohort_spec(excluded_a, lambda u: u["scorable"]) != cohort_spec(
        excluded_b, lambda u: u["scorable"],
    )
    def baseline(unit):
        return unit["scorable"] and unit["kind"] == "baseline"

    assert cohort_spec(complete, baseline) != cohort_spec(baseline_gap, baseline)
    changes = FUNCTIONS["AIQSnapshotChangesV1"]
    for name in ("PlannedCohort", "ScoredCohort", "BaselineCohort"):
        assert f"{name} != prev({name})" in changes
    units = FUNCTIONS["AIQUnitPairV1"]
    assert "join kind=fullouter Previous on Agent, LogicalVersion, Kind, ExpectedIssueAlias" in units
    assert "M = iff(Scorable and Kind == 'issue', ExpectedIssues - CorrectIssues, long(null))" in units
    assert "isnull(CurrentPresent), 'not planned'" in units
    assert "not(CurrentScorable), 'excluded'" in units
    assert "DeltaM = M - PreviousM" in units


def test_fourteen_day_defaults_and_static_choices_are_valid_without_publications():
    parameters = {item.get("variableName", "_dates"): item for item in DASHBOARD["parameters"]}
    assert parameters["_dates"]["defaultValue"] == {"count": 14, "kind": "dynamic", "unit": "days"}
    assert parameters["_region"]["dataSource"] == {
        "kind": "static", "values": [{"value": "swedencentral", "displayText": "Sweden Central"}],
    }
    policy = parameters["_policy"]
    assert policy["dataSource"]["kind"] == "static"
    assert {item["value"] for item in policy["dataSource"]["values"]} == {
        SCORING_POLICY.version, LEGACY_SCORING_POLICY.version,
    }
    assert policy["defaultValue"] == {"kind": "value", "value": SCORING_POLICY.version}
    assert policy["selectionType"] == "single"
    for variable in ("_snapshot", "_currentAgent"):
        assert parameters[variable]["defaultValue"] == {"kind": "all"}
        query = parameters[variable]["dataSource"]["query"]
        assert "AIQSnapshotRunsV1(_startTime, _endTime, _region, _policy" in query
        assert "AIQSnapshotChangesV1" not in query and "AIQSnapshotsV1" not in query
    assert snapshot_spec([]) == {}
    existing_v1 = row(4, policy=LEGACY_SCORING_POLICY)
    assert not [value for value in snapshot_spec([existing_v1]).values()
                if value["ScoringPolicy"] == policy["defaultValue"]["value"]]


def test_empty_state_keeps_score_null_and_does_not_hide_missing_function_errors():
    latest = DASHBOARD["tiles"][0]["query"]
    assert "Publication = 'No official public-safe publications" in latest
    assert "QualityScore = real(null)" in latest and "M = long(null)" in latest
    assert "where toscalar(Latest | count) == 0" in latest
    metadata = next(tile["query"] for tile in DASHBOARD["tiles"]
                    if "Metadata = bag_pack" in tile["query"])
    assert "where toscalar(Selected | count) == 0" in metadata
    assert "Metadata = dynamic(null)" in metadata
    for tile in DASHBOARD["tiles"][:-3]:
        query = tile["query"]
        assert "isfuzzy" not in query and "AIQDaily" not in query
        assert "best_effort" not in query


def test_bounded_identity_reads_preserve_cross_filter_conflicts_and_replay_order():
    reports = FUNCTIONS["AIQReportsV1"]
    candidates, reconciliation = reports.split("    QualityReportsV1\n", 1)
    for predicate in (
        "ReportDate >= startofday(startDate)", "ReportDate <= startofday(endDate)",
        "tolower(replace_string(Region, ' ', '')) == regionKey",
        "tostring(Payload.scoring_policy.version) == scoringPolicy",
        "isnull(runIds) or FrameworkRunId in (runIds)",
    ):
        assert predicate in candidates
        assert predicate not in reconciliation
    assert "FrameworkRunId in (Candidates)" in reconciliation
    assert "arg_min(PublishedAt, *) by FrameworkRunId" in reconciliation
    assert "array_length(ContentHashes) == 1" in reconciliation
    requested = row(20)
    outside_filter_conflict = dict(
        requested, ReportDate="2026-09-01", Region="synthetic-other-region", ContentHash="c" * 64,
    )
    assert snapshot_spec([requested, outside_filter_conflict]) == {}


def test_only_selected_run_ids_are_expanded_and_empty_selection_is_not_all_history():
    runs = FUNCTIONS["AIQSnapshotRunsV1"]
    assert "AIQRunsV1(iff(includePrevious, datetime(null), startDate), endDate, regionKey, scoringPolicy)" in runs
    assert "mv-expand" not in runs and "AIQUnitsV1" not in runs
    snapshots = FUNCTIONS["AIQSnapshotsV1"]
    assert "let Runs = materialize(AIQSnapshotRunsV1(startDate, endDate, regionKey, scoringPolicy, includePrevious))" in snapshots
    assert "let RunIds = toscalar(Runs | summarize make_set(FrameworkRunId))" in snapshots
    assert "let Cohorts = AIQUnitsV1(RunIds)" in snapshots
    units = FUNCTIONS["AIQUnitsV1"]
    assert units.index("AIQReportsV1(datetime(null), datetime(null), '', '', runIds)") < units.index("mv-expand Unit")
    assert "AIQUnitsV1(runIds)" in FUNCTIONS["AIQFindingsV1"]
    pair = FUNCTIONS["AIQUnitPairV1"]
    assert "materialize(AIQUnitsV1(pack_array(frameworkRunId, previousRunId)))" in pair
    assert "let Current = PairUnits" in pair and "let Previous = PairUnits" in pair
    assert "AIQSnapshotChangesV1" not in pair and "AIQSnapshotsV1" not in pair
    queries = [tile["query"] for tile in DASHBOARD["tiles"][:-3]]
    queries += [parameter["dataSource"]["query"] for parameter in DASHBOARD["parameters"]
                if parameter.get("variableName") in ("_snapshot", "_currentAgent")]
    for query in queries:
        assert not re.search(r"\bAIQ(?:Reports|Runs|Units|Findings|Snapshots|SnapshotChanges|SnapshotRuns)V1\(\)", query)
        if "AIQUnitsV1(RunIds)" in query:
            assert "coalesce(toscalar(Selected | project pack_array(FrameworkRunId, PreviousRunId)), dynamic([]))" in query
    assert "AIQSnapshotChangesV1" not in DASHBOARD["tiles"][0]["query"]
    assert "AIQSnapshotChangesV1" not in DASHBOARD["tiles"][1]["query"]
    assert "AIQSnapshotRunsV1(CurrentDate, CurrentDate, CurrentRegion, CurrentPolicy, true)" in FUNCTIONS["AIQUnitChangesV1"]


def window_spec(rows, start, end, *, include_previous):
    """Reference selection before unit expansion; deliberately not KQL execution."""
    daily = snapshot_spec(rows)
    visible = {key: value for key, value in daily.items() if start <= key[-1] <= end}
    if not include_previous:
        return visible
    for series in {key[:-1] for key in visible}:
        previous = [key for key in daily if key[:-1] == series and key[-1] < start]
        if previous:
            key = max(previous)
            visible[key] = daily[key]
    return visible


def test_bounded_cohorts_retain_exact_predecessor_outside_fourteen_day_window():
    earlier, predecessor, first, latest = row(1), row(2), row(20), row(25)
    no_visible_series = row(3, revision=1, policy=LEGACY_SCORING_POLICY)
    rows = [earlier, predecessor, first, latest, no_visible_series]
    bounded = window_spec(rows, "2026-09-12", "2026-09-25", include_previous=True)
    assert [bounded[key]["ReportDate"] for key in sorted(bounded)] == [
        "2026-09-02", "2026-09-20", "2026-09-25",
    ]
    assert len(window_spec(rows, "2026-09-12", "2026-09-25", include_previous=False)) == 2
    assert window_spec(rows, "2026-09-26", "2026-09-30", include_previous=True) == {}
    runs = FUNCTIONS["AIQSnapshotRunsV1"]
    assert "ReportDate < startofday(startDate)" in runs
    assert "arg_max(ReportDate, *) by RegionKey, ScoringPolicy, CoveragePolicy" in runs
    assert "join kind=leftsemi (Visible | distinct RegionKey, ScoringPolicy, CoveragePolicy)" in runs
    changes = FUNCTIONS["AIQSnapshotChangesV1"]
    assert "AIQSnapshotsV1(startDate, endDate, regionKey, scoringPolicy, true)" in changes
    assert changes.index("prev(PlannedCohort)") < changes.index("| where isnull(startDate)")


@pytest.mark.parametrize("optional,expected", [
    ({}, (None, None, "Legacy formula not recorded")),
    ({"QualityScoreFormula": ""}, (None, None, "Legacy formula not recorded")),
    ({"IssuesMissing": 2, "DuplicateCards": 3, "QualityScoreFormula": "stored-legacy-formula"},
     (2, 3, "stored-legacy-formula")),
])
def test_legacy_optional_columns_preserve_missing_values_without_inventing_counts(optional, expected):
    trend, daily, _ = DASHBOARD["tiles"][-3:]
    formula = "coalesce(tostring(column_ifexists('QualityScoreFormula', '')), 'Legacy formula not recorded')"
    assert formula in trend["query"] and formula in daily["query"]
    for field in ("IssuesMissing", "DuplicateCards"):
        assert f"{field} = tolong(column_ifexists('{field}', long(null)))" in daily["query"]
    source = {
        "IssuesCorrect": 4, "IssuesExpected": 10, "IssuesPartial": 3,
        "QualityFailures": 2, "UnverifiedCards": 7, **optional,
    }
    # Synthetic schema reference, not KQL execution: older statistics are not substitutes.
    assert (
        source.get("IssuesMissing"), source.get("DuplicateCards"),
        source.get("QualityScoreFormula") or "Legacy formula not recorded",
    ) == expected
    assert "IssuesExpected -" not in daily["query"]
    assert all(field not in daily["query"] for field in (
        "IssuesPartial", "QualityFailures", "UnverifiedCards", "IssuesIncorrect",
    ))


@pytest.mark.parametrize("source,expected", [
    ({"Result": "legacy-recorded-result"}, "legacy-recorded-result"),
    ({"Outcome": "recorded-outcome", "Result": "older-result"}, "recorded-outcome"),
    ({}, "Not recorded"),
])
def test_legacy_outcome_column_rename_preserves_recorded_meaning(source, expected):
    issues = DASHBOARD["tiles"][-1]
    assert "OutcomeOrResult = tostring(column_ifexists('Outcome', column_ifexists('Result', 'Not recorded')))" in issues["query"]
    assert source.get("Outcome", source.get("Result", "Not recorded")) == expected
