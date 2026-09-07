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
FUNCTIONS = dict(re.findall(r"\) (AIQ\w+)\([^)]*\) \{\n(.*?)\n\}", KQL, re.DOTALL))
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
            assert "RegionKey == _region and ScoringPolicy == _policy" in tile["query"]


def test_query_dependencies_are_defined_current_read_models_not_raw_or_legacy_data():
    queries = [tile["query"] for tile in DASHBOARD["tiles"]]
    queries += [parameter["dataSource"]["query"] for parameter in DASHBOARD["parameters"]
                if "dataSource" in parameter]
    queries += list(FUNCTIONS.values())
    for query in queries:
        assert set(re.findall(r"\b(AIQ\w+)\(", query)) <= FUNCTIONS.keys()
    current = KQL.split("// Current framework data", 1)[1]
    assert "DailyQualityPublications" not in current
    for name in ("AIQSnapshotsV1", "AIQSnapshotChangesV1", "AIQUnitChangesV1"):
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
    snapshot = FUNCTIONS["AIQSnapshotsV1"]
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
    assert "| where" not in changes


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
    units = FUNCTIONS["AIQUnitChangesV1"]
    assert "join kind=fullouter Previous on Agent, LogicalVersion, Kind, ExpectedIssueAlias" in units
    assert "M = iff(Scorable and Kind == 'issue', ExpectedIssues - CorrectIssues, long(null))" in units
    assert "isnull(CurrentPresent), 'not planned'" in units
    assert "not(CurrentScorable), 'excluded'" in units
    assert "DeltaM = M - PreviousM" in units
