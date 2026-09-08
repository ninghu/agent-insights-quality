import json
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.errors import QualityError
from agent_insights_quality.public_artifacts import public_markdown, verify_artifact
from agent_insights_quality.publication import build_public_report
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, PlannedUnit, UnitResult, aggregate_results,
)
from agent_insights_quality.selection import select_daily

ROOT = Path(__file__).resolve().parents[2]


def document(root, *, incorrect=False, categorized=False):
    day = date(2026, 9, 4)
    targets = select_daily(load_catalog(root), day)
    plan = tuple(PlannedUnit(
        item.unit_id, None if item.is_baseline else item.unit_id.logical_version,
        item.expectation["category"] if categorized and not item.is_baseline else None,
    ) for item in targets)
    actual = []
    for index, unit in enumerate(plan):
        cards = ()
        if unit.is_issue:
            cards = (CardVerdict(
                "card-0001", CoreVerdict.INCORRECT if incorrect and index == 1 else CoreVerdict.CORRECT,
                unit.expected_issue_alias,
            ),)
        actual.append(UnitResult(unit.unit_id, cards))
    result = aggregate_results(plan, actual)
    return build_public_report(
        result, allowed_units=plan, report_date=day.isoformat(),
        framework_run_id="daily-synthetic", source_commit="a" * 40,
        region="SwedenCentral",
    )


@pytest.fixture
def repository(tmp_path, monkeypatch):
    (tmp_path / "catalogs").mkdir()
    for name in ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml"):
        shutil.copyfile(ROOT / "catalogs" / name, tmp_path / "catalogs" / name)
    frozen_source_catalog(tmp_path, monkeypatch)
    return tmp_path


def test_public_validator_has_no_report_writer(repository):
    from agent_insights_quality import public_artifacts
    assert not hasattr(public_artifacts, "write_public_report")
    public_markdown(repository, document(repository))
    assert not (repository / "reports").exists()


def test_historical_public_rendering_remains_deterministic(repository):
    value = document(repository)
    markdown = public_markdown(repository, value)
    assert public_markdown(repository, value) == markdown
    assert "Report date: " + value["report_date"] in markdown
    assert "Region: " + value["region"] in markdown
    assert "Source commit: " + value["source_commit"] in markdown
    targets = select_daily(load_catalog(repository), date.fromisoformat(value["report_date"]))
    for target in targets:
        assert target.unit_id.logical_version in markdown
        assert f'id="{target.unit_id.agent}"' in markdown
    assert "No unexpected finding" not in markdown
    assert "Expected defect detected" not in markdown
    assert "| 1. Matched |" in markdown
    assert "card-0001: expected_detection (scored)" not in markdown


def frozen_source_catalog(repository, monkeypatch):
    from agent_insights_quality import public_artifacts

    snapshots = {
        "a" * 40 + f":catalogs/{filename}":
            (repository / "catalogs" / filename).read_text(encoding="utf-8")
        for filename in ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml")
    }
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "merge-base":
            return SimpleNamespace(returncode=0)
        assert command[:2] == ["git", "show"]
        return SimpleNamespace(returncode=0, stdout=snapshots[command[2]])

    monkeypatch.setattr(public_artifacts.subprocess, "run", run)
    return calls


def test_categorized_artifact_uses_its_reviewed_plan_without_backfilling_legacy(repository, monkeypatch):
    from agent_insights_quality.public_artifacts import approved_document

    calls = frozen_source_catalog(repository, monkeypatch)
    old = document(repository)
    current = document(repository, categorized=True)
    _, legacy, legacy_plan = approved_document(repository, old)
    _, measured, plan = approved_document(repository, current)
    assert legacy.category_breakdown is None
    assert all(unit.category is None for unit in legacy_plan)
    assert measured.category_breakdown.to_dict() == current["report"]["category_breakdown"]
    assert all(unit.category is not None for unit in plan if unit.is_issue)
    assert public_markdown(repository, current) == public_markdown(repository, current)
    assert old["report"] == legacy.to_dict()
    assert calls[0][1] == "merge-base" and calls[1][1] == "show"


def test_current_catalog_reassignment_preserves_frozen_historical_categories(repository, monkeypatch):
    from agent_insights_quality.public_artifacts import approved_document

    frozen_source_catalog(repository, monkeypatch)
    current = document(repository, categorized=True)
    _, original, plan = approved_document(repository, current)
    before = public_markdown(repository, current)
    first = next(unit for unit in plan if unit.is_issue)
    path = repository / "catalogs" / "ISSUE_CATALOG.yaml"
    catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    issue = next(issue for issue in catalog["issues"] if issue["id"] == first.expected_issue_alias)
    issue["category"] = "latency" if first.category != "latency" else "hallucinations"
    path.write_text(yaml.safe_dump(catalog), encoding="utf-8")
    _, restored, historical_plan = approved_document(repository, current)
    assert historical_plan == plan and restored == original
    assert public_markdown(repository, current) == before
    assert current["report"] == original.to_dict()


@pytest.mark.parametrize("failure", ["untrusted", "missing_catalog", "invalid_catalog"])
def test_historical_categories_never_fall_back_when_source_is_unavailable(repository, monkeypatch, failure):
    from agent_insights_quality import public_artifacts

    current = document(repository, categorized=True)

    def run(command, **kwargs):
        if command[1] == "merge-base":
            return SimpleNamespace(returncode=1 if failure == "untrusted" else 0)
        return SimpleNamespace(returncode=1 if failure == "missing_catalog" else 0, stdout="{}")

    monkeypatch.setattr(public_artifacts.subprocess, "run", run)
    with pytest.raises(QualityError, match="public_report_source"):
        public_artifacts.approved_document(repository, current)


def test_historical_validator_reads_but_does_not_rewrite(repository):
    value = document(repository)
    path, markdown = repository / "report.json", repository / "report.md"
    path.write_text(json.dumps(value))
    markdown.write_text(public_markdown(repository, value))
    before = path.read_bytes(), markdown.read_bytes()
    verify_artifact(repository, path, markdown, "reports/daily/2026/09/04/report.json")
    assert (path.read_bytes(), markdown.read_bytes()) == before
    markdown.write_text("changed")
    with pytest.raises(QualityError, match="public_report_rendering_mismatch"):
        verify_artifact(repository, path, markdown)


def test_shadowed_public_fields_are_rejected(repository):
    value = document(repository)
    text = json.dumps(value)
    path = repository / "input.json"
    path.write_text('{"region":"unapproved",' + text[1:], encoding="utf-8")
    with pytest.raises(QualityError, match="public_report_invalid"):
        verify_artifact(repository, path)


@pytest.mark.parametrize("categorized", [False, True])
def test_inventory_expansion_does_not_reselect_historical_daily_units(repository, monkeypatch, categorized):
    from agent_insights_quality.public_artifacts import approved_document

    paths = [repository / "catalogs" / name for name in ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml")]
    current = [path.read_text(encoding="utf-8") for path in paths]
    agents, issues = [yaml.safe_load(text) for text in current]
    additions = {f"issue-{number:03d}" for number in range(37, 41)}
    for agent in agents["agents"]:
        agent["issue_ids"] = [identity for identity in agent["issue_ids"] if identity not in additions]
    issues["issues"] = [issue for issue in issues["issues"] if issue["id"] not in additions]
    for path, catalog in zip(paths, (agents, issues), strict=True):
        path.write_text(yaml.safe_dump(catalog), encoding="utf-8")
    frozen_source_catalog(repository, monkeypatch)
    historical = document(repository, categorized=categorized)
    _, measured, plan = approved_document(repository, historical)
    markdown = public_markdown(repository, historical)
    for path, text in zip(paths, current, strict=True):
        path.write_text(text, encoding="utf-8")
    selected_now = select_daily(load_catalog(repository), date.fromisoformat(historical["report_date"]))
    assert {unit.unit_id for unit in plan} != {target.unit_id for target in selected_now}
    _, restored, frozen_plan = approved_document(repository, historical)
    assert len(frozen_plan) == 25 and frozen_plan == plan
    assert restored == measured and historical["report"] == measured.to_dict()
    assert public_markdown(repository, historical) == markdown
