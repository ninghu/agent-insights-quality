"""Date and dependency selection has no provider or state-store side effects."""

import copy
import json
import subprocess
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.selection import (
    LastTest, SourceChanges, deployment_inputs, evaluation_inputs,
    git_changed_paths, git_source_changes, select_daily, select_staging, traffic_inputs,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(ROOT)


def records(catalog):
    return {target.key: LastTest("source-one", "PASS", "2026-09-01") for target in catalog.targets}


@pytest.mark.parametrize("start", [date(2026, 9, 3), date(2026, 9, 4), date(2026, 12, 31)])
def test_rotation_covers_inventory_across_consecutive_weekdays(catalog, start):
    following = start + timedelta(days=1)
    while following.weekday() >= 5:
        following += timedelta(days=1)
    days = [select_daily(catalog, day) for day in (start, following)]
    for selected in days:
        assert len(selected) == len({target.key for target in selected}) == 25
        for position, name in enumerate(catalog.agents):
            lane = selected[position * 5:(position + 1) * 5]
            assert lane[0].is_baseline
            assert all(target.unit_id.agent == name for target in lane)
            assert sum(not target.is_baseline for target in lane) == 4
    assert {target.key for day in days for target in day} == {target.key for target in catalog.targets}
    reordered = replace(catalog, targets=tuple(reversed(catalog.targets)))
    assert select_daily(reordered, start) == days[0]


@pytest.mark.parametrize("day", [date(2026, 9, 5), date(2026, 9, 6)])
def test_private_weekend_planning_does_not_rewrite_the_execution_date(catalog, day):
    with pytest.raises(ValueError, match="weekday"):
        select_daily(catalog, day)
    planned = select_daily(catalog, day, test_run=True)
    assert len(planned) == 25
    assert planned == select_daily(catalog, day, test_run=True)
    assert day in (date(2026, 9, 5), date(2026, 9, 6))


def test_missing_full_incomplete_and_unchanged_failure(catalog):
    assert len(select_staging(catalog)) == 41
    prior = records(catalog)
    first = catalog.targets[0].key
    prior[first] = LastTest("source-one", "FAIL", "2026-09-01")
    assert not select_staging(catalog, last_tests=prior)
    assert len(select_staging(catalog, last_tests=prior, full=True)) == 41
    prior[first] = LastTest("source-one", "INCOMPLETE", "2026-09-01")
    missing = catalog.targets[-1].key
    del prior[missing]
    selected = select_staging(catalog, last_tests=prior)
    assert {item.target.key for item in selected} == {first, missing}
    assert {item.reasons for item in selected} == {("incomplete",), ("missing",)}
    assert prior[first].tested_at == "2026-09-01"


@pytest.mark.parametrize("relative,expected,action", [
    ("agents/finance-agent/issues/issue-013/source/new_module.py", {"finance-agent/issue-013"}, "traffic"),
    ("agents/finance-agent/issues/issue-013/traffic.json", {"finance-agent/issue-013"}, "traffic"),
    ("agents/weather-agent/v0/definition.json", {"weather-agent/v0"}, "traffic"),
    ("agents/finance-agent/v0/source/app.py", {"finance-agent/v0"}, "traffic"),
    ("agents/finance-agent/v0/requirements.txt", {"finance-agent"}, "traffic"),
    ("agents/travel-agent/v0/package.py", set(), "traffic"),
    ("agents/support-ticket-agent/v0/Dockerfile", {"support-ticket-agent"}, "traffic"),
    ("agents/support-ticket-agent/v0/container.yaml", set(), "traffic"),
    ("agents/finance-agent/issues/issue-013/implementation.yaml", {"finance-agent/issue-013"}, "reassess"),
    ("agents/support-ticket-agent/issues/issue-029/implementation.yaml", {"support-ticket-agent/issue-029"}, "reassess"),
    ("src/agent_insights_quality/providers/artifacts.py", {"all"}, "traffic"),
    ("src/agent_insights_quality/providers/hosted.py", {"hosted"}, "traffic"),
    ("src/agent_insights_quality/providers/acr.py", {"support-ticket-agent"}, "traffic"),
    ("src/agent_insights_quality/providers/container_environment.py", {"support-ticket-agent"}, "traffic"),
    ("src/agent_insights_quality/invocation_context.py", {"all"}, "traffic"),
    ("src/agent_insights_quality/providers/runtime.py", {"all"}, "traffic"),
    ("src/agent_insights_quality/providers/transport.py", {"all"}, "traffic"),
    ("src/agent_insights_quality/providers/sol.py", {"all"}, "reassess"),
    ("src/agent_insights_quality/cli.py", set(), "traffic"),
    ("src/agent_insights_quality/assessment.py", {"all"}, "reassess"),
    ("src/agent_insights_quality/staging_policy.py", {"all"}, "reassess"),
    ("src/agent_insights_quality/assessment_partition.py", {"all"}, "reassess"),
    ("src/agent_insights_quality/telemetry.py", {"all"}, "reassess"),
    ("src/agent_insights_quality/prompts/staging.md", {"all"}, "reassess"),
    ("src/agent_insights_quality/prompts/daily.md", set(), "traffic"),
    ("src/agent_insights_quality/prompts/unrelated.md", set(), "traffic"),
    ("src/agent_insights_quality/evidence.py", set(), "traffic"),
    ("catalogs/ISSUE_CATALOG.yaml", {"all"}, "reassess"),
    ("README.md", set(), "traffic"),
    ("src/agent_insights_quality/results.py", set(), "traffic"),
    ("reports/synthetic.md", set(), "traffic"),
])
def test_dependencies_select_only_affected_work(catalog, relative, expected, action):
    selected = select_staging(catalog, last_tests=records(catalog), changed_paths=[relative])
    if expected == {"all"}:
        expected = {target.key for target in catalog.targets}
    elif expected == {"hosted"}:
        expected = {target.key for target in catalog.targets if not target.is_prompt}
    elif expected and "/" not in next(iter(expected)):
        expected = {target.key for target in catalog.for_agent(next(iter(expected)))}
    assert {item.target.key for item in selected} == expected
    assert all(item.action == action for item in selected)


def test_per_record_revision_and_explicit_verifier_dependencies(catalog):
    prior = records(catalog)
    target = catalog.target("finance-agent/issue-013")
    prior[target.key] = LastTest("older-source", "FAIL", "2026-08-01")
    selected = select_staging(catalog, last_tests=prior, changes_by_revision={
        "source-one": [],
        "older-source": [target.version_root / "source" / "finance.py"],
    })
    assert [item.target.key for item in selected] == [target.key]
    with pytest.raises(ValueError, match="comparison"):
        select_staging(catalog, last_tests=prior, changes_by_revision={})
    selected = select_staging(
        catalog, last_tests=prior, changed_paths=["src/custom_verifier.py"],
        evaluation_paths=["src/custom_verifier.py"],
    )
    assert len(selected) == 41 and all(item.action == "reassess" for item in selected)


def test_expectation_only_traffic_edits_do_not_retraffic(catalog):
    path = Path("agents/finance-agent/issues/issue-013/traffic.json")
    changes = SourceChanges((path,), (path,))
    selected = select_staging(catalog, last_tests=records(catalog), changed_paths=changes)
    assert len(selected) == 1
    assert selected[0].target.key == "finance-agent/issue-013"
    assert selected[0].action == "reassess"
    selected = select_staging(
        catalog, last_tests=records(catalog), changed_paths=[path],
        changes_by_revision={"source-one": changes},
    )
    assert selected[0].action == "traffic"


def test_scoped_issue_catalog_change_does_not_reassess_unchanged_units(catalog):
    path = Path("catalogs/ISSUE_CATALOG.yaml")
    changes = SourceChanges((path,), evaluation_scopes=((path, ("support-ticket-agent/issue-031",)),))
    selected = select_staging(catalog, last_tests=records(catalog), changed_paths=changes)
    assert [(item.target.key, item.action) for item in selected] == [
        ("support-ticket-agent/issue-031", "reassess"),
    ]
    assert len(select_staging(
        catalog, last_tests=records(catalog), changed_paths=(path,),
        changes_by_revision={"source-one": changes},
    )) == 41
    assert len(select_staging(
        catalog, last_tests=records(catalog), changed_paths=changes,
        changes_by_revision={"source-one": (Path("src/agent_insights_quality/assessment.py"),)},
    )) == 41


@pytest.mark.parametrize("mutation,targets", [
    ("entry", ("support-ticket-agent/issue-031",)),
    ("reorder", ()),
    ("global", None),
    ("owner", None),
    ("added", None),
])
def test_git_catalog_scope_uses_entries_without_narrowing_global_or_inventory_edits(tmp_path, catalog, mutation, targets):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()
    git("init", "--quiet")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "commit.gpgsign", "false")
    relative = Path("catalogs/ISSUE_CATALOG.yaml")
    path = tmp_path / relative
    path.parent.mkdir()
    document = copy.deepcopy(catalog._documents[1])
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    git("add", "--all")
    git("commit", "--quiet", "-m", "Synthetic reviewed inventory")
    first = git("rev-parse", "HEAD")
    issue = next(item for item in document["issues"] if item["id"] == "issue-031")
    if mutation == "entry":
        issue["expected_fix"] = "Synthetic clarified existing healthy behavior."
    elif mutation == "reorder":
        document["issues"].reverse()
    elif mutation == "global":
        document["selection"]["issues_per_agent_daily"] = 3
    elif mutation == "owner":
        issue["agent"] = "weather-agent"
    else:
        document["issues"].append({**issue, "id": "issue-037"})
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    git("add", "--all")
    git("commit", "--quiet", "-m", "Synthetic catalog change")
    changes = git_source_changes(tmp_path, first)
    expected = () if targets is None else ((relative, targets),)
    assert changes.evaluation_scopes == expected
    selected = select_staging(catalog, last_tests=records(catalog), changed_paths=changes)
    assert len(selected) == (41 if targets is None else len(targets))


def test_catalog_scope_rejects_duplicate_entries_and_uncompared_scope_paths():
    from agent_insights_quality.selection import _issue_evaluation_scope
    issue = {"id": "issue-031", "agent": "support-ticket-agent"}
    with pytest.raises(ValueError, match="Duplicate issue"):
        _issue_evaluation_scope({"issues": [issue]}, {"issues": [issue, issue]})
    with pytest.raises(ValueError, match="compared paths"):
        SourceChanges((), evaluation_scopes=((Path("catalogs/ISSUE_CATALOG.yaml"), ()),))


def test_deployment_inputs_exclude_traffic_and_verifiers(catalog):
    for target in catalog.targets:
        deployment = set(deployment_inputs(target))
        assert not deployment & set(traffic_inputs(target))
        assert all(path.is_absolute() for path in deployment)
        assert target.version_root / "traffic.json" not in deployment
        assert evaluation_inputs(target)
    issue = catalog.target("finance-agent/issue-013")
    assert issue.baseline_root / "source" not in deployment_inputs(issue)
    assert issue.baseline_root / "requirements.txt" in deployment_inputs(issue)
    for target in catalog.targets:
        paths = deployment_inputs(target)
        assert all(path.exists() for path in paths)
        assert ROOT / "src" / "agent_insights_quality" / "cli.py" not in paths
        assert target.baseline_root / "package.py" not in paths
        assert target.baseline_root / "container.yaml" not in paths
        assert target.version_root / "implementation.yaml" not in paths
        assert target.version_root / "implementation.yaml" in evaluation_inputs(target)
        evaluator = ROOT / "src" / "agent_insights_quality"
        for path in (
            evaluator / "assessment_partition.py",
            evaluator / "providers" / "sol.py",
            evaluator / "prompts" / "staging.md",
        ):
            assert path not in paths and path in evaluation_inputs(target)
        assert evaluator / "prompts" / "daily.md" not in evaluation_inputs(target)


def test_git_bound_deployment_revision_changes_only_for_actual_inputs(catalog, tmp_path):
    from agent_insights_quality.runner import deployment_revision

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "commit.gpgsign", "false")
    local = replace(catalog, root=tmp_path)
    targets = tuple(
        replace(
            catalog.target(key),
            version_root=tmp_path / catalog.target(key).version_root.relative_to(catalog.root),
            baseline_root=tmp_path / catalog.target(key).baseline_root.relative_to(catalog.root),
        )
        for key in (
            "weather-agent/v0", "finance-agent/issue-013", "support-ticket-agent/issue-029",
        )
    )
    for path in {path for target in targets for path in deployment_inputs(target)}:
        file = path / "app.py" if path.name == "source" else path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic initial input\n")
    git("add", "--all")
    git("commit", "--quiet", "-m", "Synthetic initial inputs")
    expected = {target.key: deployment_revision(local, target) for target in targets}
    for path, affected in (
        ("src/agent_insights_quality/cli.py", set()),
        ("src/agent_insights_quality/providers/hosted.py", {"hosted_code", "hosted_custom_container"}),
        ("src/agent_insights_quality/providers/artifacts.py", {"prompt", "hosted_code", "hosted_custom_container"}),
        ("src/agent_insights_quality/providers/acr.py", {"hosted_custom_container"}),
        ("src/agent_insights_quality/providers/container_environment.py", {"hosted_custom_container"}),
        ("agents/finance-agent/v0/package.py", set()),
        ("agents/support-ticket-agent/issues/issue-029/implementation.yaml", set()),
        ("src/agent_insights_quality/telemetry.py", set()),
        ("src/agent_insights_quality/assessment_partition.py", set()),
        ("src/agent_insights_quality/providers/sol.py", set()),
        ("src/agent_insights_quality/prompts/staging.md", set()),
        ("src/agent_insights_quality/prompts/daily.md", set()),
    ):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic changed input\n")
        git("add", "--all")
        git("commit", "--quiet", "-m", "Synthetic change")
        revision = git("rev-parse", "HEAD")
        for target in targets:
            if target.agent_type in affected:
                expected[target.key] = revision
            assert deployment_revision(local, target) == expected[target.key]


def test_git_comparison_detects_expectation_only_and_request_changes(tmp_path):
    def git(*args):
        result = subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "commit.gpgsign", "false")
    relative = Path("agents", "synthetic-agent", "v0", "traffic.json")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    document = {
        "contract_version": "2.0",
        "requests": [{"id": "synthetic", "request": {"body": {"input": "synthetic"}},
                      "expected": {"behavior": "Original expectation"}}],
    }

    def commit(document):
        path.write_text(json.dumps(document), encoding="utf-8")
        git("add", "--all")
        git("commit", "--quiet", "-m", "Synthetic fixture")
        return git("rev-parse", "HEAD")

    first = commit(document)
    changed = copy.deepcopy(document)
    changed["requests"][0]["expected"]["behavior"] = "Revised expectation"
    second = commit(changed)
    assert git_source_changes(tmp_path, first, second) == SourceChanges((relative,), (relative,))
    changed["requests"][0]["request"]["body"]["input"] = "Changed synthetic input"
    third = commit(changed)
    assert git_source_changes(tmp_path, second, third) == SourceChanges((relative,))
    renamed = path.with_name("renamed.json")
    path.rename(renamed)
    git("add", "--all")
    git("commit", "--quiet", "-m", "Synthetic rename")
    assert set(git_changed_paths(tmp_path, third)) == {relative, relative.with_name("renamed.json")}
    with pytest.raises(subprocess.CalledProcessError):
        git_changed_paths(tmp_path, "--not-a-revision")


def test_unknown_records_and_outside_paths_are_not_silently_ignored(catalog):
    with pytest.raises(ValueError, match="unknown target"):
        select_staging(catalog, last_tests={"unknown": LastTest("source", "PASS", "2026-09-01")})
    with pytest.raises(ValueError, match="escapes"):
        select_staging(catalog, changed_paths=["../outside"])
