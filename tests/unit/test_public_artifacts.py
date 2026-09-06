import json
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.errors import QualityError
from agent_insights_quality.public_artifacts import public_markdown, verify_artifact
from agent_insights_quality.publication import build_public_report
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, PlannedUnit, UnitResult, aggregate_results,
)
from agent_insights_quality.selection import select_daily

ROOT = Path(__file__).resolve().parents[2]


def document(root, *, incorrect=False):
    day = date(2026, 9, 4)
    targets = select_daily(load_catalog(root), day)
    plan = tuple(PlannedUnit(
        item.unit_id, None if item.is_baseline else item.unit_id.logical_version,
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
def repository(tmp_path):
    (tmp_path / "catalogs").mkdir()
    for name in ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml"):
        shutil.copyfile(ROOT / "catalogs" / name, tmp_path / "catalogs" / name)
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
    assert "Expected issue: Detected" in markdown
    assert "card-0001: expected_detection (scored)" not in markdown


def test_historical_validator_reads_but_does_not_rewrite(repository, monkeypatch):
    from agent_insights_quality import public_artifacts
    monkeypatch.setattr(public_artifacts.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
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
