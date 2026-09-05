import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.errors import QualityError
from agent_insights_quality.public_artifacts import public_markdown, verify_artifact, write_public_report
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


def test_email_test_never_writes_public_files(repository):
    value = document(repository)
    with pytest.raises(QualityError, match="test_publication_forbidden"):
        write_public_report(repository, value, test_run=True)
    assert not (repository / "reports").exists()


def test_public_files_share_one_validated_envelope_and_rendering(repository):
    value = document(repository)
    paths = write_public_report(repository, value, test_run=False)
    assert len(paths) == 4
    dated = repository / "reports" / "daily" / "2026" / "09" / "04"
    assert json.loads((dated / "report.json").read_text()) == value
    assert (dated / "report.md").read_text() == public_markdown(repository, value)
    assert (repository / "reports" / "latest.json").read_bytes() == (dated / "report.json").read_bytes()
    assert write_public_report(repository, value, test_run=False) == paths
    markdown = (dated / "report.md").read_text()
    assert "Report date: " + value["report_date"] in markdown
    assert "Region: " + value["region"] in markdown
    assert "Source commit: " + value["source_commit"] in markdown
    targets = select_daily(load_catalog(repository), date.fromisoformat(value["report_date"]))
    for target in targets:
        if not target.is_baseline:
            assert target.expectation["title"] in markdown
            assert target.expectation["root_cause"] in markdown
            assert target.expectation["expected_fix"] in markdown
    assert "card-0001: expected_detection (scored)" in markdown


def test_dated_report_is_not_overwritten_by_a_different_measurement(repository):
    write_public_report(repository, document(repository), test_run=False)
    with pytest.raises(QualityError, match="public_report_conflict"):
        write_public_report(repository, document(repository, incorrect=True), test_run=False)


def test_shadowed_public_fields_are_rejected(repository):
    value = document(repository)
    text = json.dumps(value)
    path = repository / "input.json"
    path.write_text('{"region":"unapproved",' + text[1:], encoding="utf-8")
    with pytest.raises(QualityError, match="public_report_invalid"):
        verify_artifact(repository, path)
