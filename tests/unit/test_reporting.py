from dataclasses import replace
from datetime import date
from html import unescape
import json
from pathlib import Path
import re
import shutil

import pytest
import yaml

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.privacy import PrivacyError
from agent_insights_quality.report_context import (
    ReportContextError, ReportMetadata, ReviewedReportContext, load_report_context,
)
from agent_insights_quality.reporting import render_html, render_json, render_markdown
from agent_insights_quality.results import (
    CardVerdict,
    CoreVerdict,
    Contribution,
    ExclusionReason,
    PlannedUnit,
    UnitId,
    UnitResult,
    aggregate_results,
)
from agent_insights_quality.selection import select_daily

ROOT = Path(__file__).resolve().parents[2]


def report(excluded=0):
    plan, actual = [], []
    for lane in range(5):
        agent = f"synthetic-agent-{lane}"
        baseline = UnitId(agent, "v0")
        plan.append(PlannedUnit(baseline))
        actual.append(UnitResult(baseline, (
            CardVerdict("card-0001", CoreVerdict.INCORRECT),
        ) if lane == 0 else ()))
        for issue in range(1, 5):
            alias = f"issue-{lane * 4 + issue:03d}"
            unit = UnitId(agent, alias)
            plan.append(PlannedUnit(unit, alias))
            actual.append(UnitResult(unit, (
                CardVerdict("card-0001", CoreVerdict.CORRECT, alias),
                CardVerdict("card-0002", CoreVerdict.CORRECT, alias),
            ) if issue == 1 else ()))
    for index in range(excluded):
        actual[index] = replace(
            actual[index], exclusion_reasons=(ExclusionReason.INCOMPLETE_EVIDENCE,),
        )
    return aggregate_results(plan, actual), tuple(plan)


@pytest.mark.parametrize("excluded,status", [(0, "Full"), (1, "Partial"), (2, "Partial"), (3, "Failed")])
def test_counts_coverage_and_exclusions_agree_across_every_renderer(excluded, status):
    result, plan = report(excluded)
    markdown = render_markdown(result, allowed_units=plan)
    html = unescape(re.sub("<[^>]+>", "", render_html(result, allowed_units=plan)))
    public = json.loads(render_json(result, allowed_units=plan))
    assert public == result.to_dict()
    assert public["status"] == status
    for rendered in (markdown, html):
        assert f"{status} - Quality score:" in rendered
        assert f"C={result.counts.correct_issues}; E_scored={result.counts.expected_issues}" in rendered
        assert f"N_scored={result.counts.noise_cards}; D_scored={result.counts.duplicate_cards}" in rendered
        assert f"{result.coverage.scored_issues}/20 planned" in rendered
        assert f"{result.coverage.scored_baselines}/5 planned" in rendered
        assert f"excluded whole units: {excluded}" in rendered
        assert "Noise weight 1; Duplicate weight 0.25" in rendered
        assert "No overall quality threshold" in rendered
        if excluded:
            assert "incomplete_evidence" in rendered
            assert "unscored noise" in rendered
        assert "Confirmed Engine gaps" in rendered
        assert "framework/infrastructure" in rendered
    assert result.score is None if excluded > 2 else result.score is not None


def test_optional_warning_does_not_change_or_block_report():
    result, plan = report()
    warning = render_html(result, allowed_units=plan, warnings=("work_item_unavailable",))
    assert "Optional work-item context is unavailable." in warning
    assert result.counts.noise_cards == 1
    assert result.team_report_eligible


def test_measured_zero_is_not_unmeasured():
    unit = UnitId("weather-agent", "issue-001")
    plan = (PlannedUnit(unit, "issue-001"),)
    zero = aggregate_results(plan, (UnitResult(unit),))
    missing = aggregate_results(plan, ())
    assert "Quality score: 0.0" in render_markdown(zero, allowed_units=plan)
    assert "Unmeasured (no quality score)" in render_html(missing, allowed_units=plan)


@pytest.fixture
def catalog_root(tmp_path):
    (tmp_path / "catalogs").mkdir()
    for name in ("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml"):
        shutil.copyfile(ROOT / "catalogs" / name, tmp_path / "catalogs" / name)
    return tmp_path


def reviewed_sample():
    baseline = PlannedUnit(UnitId("weather-agent", "v0"))
    issue = PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001")
    plan = (baseline, issue)
    actual = (
        UnitResult(baseline.unit_id),
        UnitResult(issue.unit_id, (
            CardVerdict("card-0001", CoreVerdict.INCORRECT),
        )),
    )
    return aggregate_results(plan, actual), plan


def test_catalog_bound_actionable_context_in_both_renderers(catalog_root):
    result, plan = reviewed_sample()
    context = load_report_context(catalog_root, allowed_units=plan)
    metadata = ReportMetadata("2026-09-04", "Sweden Central", "a" * 40)
    markdown = render_markdown(
        result, allowed_units=plan, report_context=context, metadata=metadata,
    )
    html = unescape(re.sub("<[^>]+>", "", render_html(
        result, allowed_units=plan, report_context=context, metadata=metadata,
    )))
    expected = load_catalog(catalog_root).target("weather-agent/issue-001").expectation
    for rendered in (markdown, html):
        assert expected["title"] in rendered
        assert expected["root_cause"] in rendered
        assert expected["expected_fix"] in rendered
        assert "agents/weather-agent/issues/issue-001/traffic.json" in rendered
        assert "agents/weather-agent/issues/issue-001/definition.json" in rendered
        assert "card-0001: noise (scored)" in rendered
        assert "no current card correctly detected it" in rendered
        assert "diagnosis" in rendered and "linkage" in rendered
        assert "catalog context describes intended defects, not proof" in rendered
        assert "ten attempts" in rendered and "setup/probe turn order" in rendered
        assert "Report date: 2026-09-04" in rendered
        assert "Region: Sweden Central" in rendered
        assert "Source commit: " + "a" * 40 in rendered
        assert str(catalog_root) not in rendered
    assert result.counts.correct_issues == 0
    assert result.counts.expected_issues == result.counts.noise_cards == 1


def test_all_catalog_units_bind_titles_and_version_owned_reproduction_without_reading_source(catalog_root):
    targets = load_catalog(catalog_root).targets
    plan = tuple(PlannedUnit(
        target.unit_id, None if target.is_baseline else target.unit_id.logical_version,
    ) for target in targets)
    contexts = load_report_context(catalog_root, allowed_units=plan).for_plan(plan)
    assert len(contexts) == 41
    assert not (catalog_root / "agents").exists()
    for target in targets:
        context = contexts[target.unit_id]
        version = target.version_root.relative_to(catalog_root).as_posix()
        assert context.traffic_path == f"{version}/traffic.json"
        assert context.source_path == version + (
            "/definition.json" if target.is_prompt else "/source/"
        )
        if not target.is_baseline:
            assert context.title == target.expectation["title"]
            assert context.expected_symptom == target.expectation["root_cause"]
            assert context.healthy_behavior == target.expectation["expected_fix"]


def test_context_cannot_come_from_model_mapping_or_another_unit(catalog_root):
    result, plan = reviewed_sample()
    with pytest.raises(ReportContextError, match="context_invalid"):
        render_html(result, allowed_units=plan, report_context={"title": "model text"})
    with pytest.raises(TypeError):
        ReviewedReportContext(title="model text")
    with pytest.raises(ReportContextError, match="identity_mismatch"):
        load_report_context(catalog_root, allowed_units=(
            PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-002"),
        ))
    with pytest.raises(ReportContextError, match="identity_mismatch"):
        load_report_context(catalog_root, allowed_units=(
            PlannedUnit(UnitId("finance-agent", "issue-001"), "issue-001"),
        ))
    subset = load_report_context(catalog_root, allowed_units=plan[1:])
    with pytest.raises(ReportContextError, match="identity_mismatch"):
        render_markdown(result, allowed_units=plan, report_context=subset)


def test_context_is_a_frozen_copy_not_live_catalog_or_mutable_payload(catalog_root):
    result, plan = reviewed_sample()
    context = load_report_context(catalog_root, allowed_units=plan)
    original = render_html(result, allowed_units=plan, report_context=context)
    copied = context.to_private_dict()
    copied["units"][1]["title"] = "unapproved model text"
    assert render_html(result, allowed_units=plan, report_context=context) == original
    path = catalog_root / "catalogs" / "ISSUE_CATALOG.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["issues"][0]["title"] = "Revised reviewed title"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert render_html(result, allowed_units=plan, report_context=context) == original
    revised = load_report_context(catalog_root, allowed_units=plan)
    assert revised.to_private_dict() != context.to_private_dict()


def test_catalog_identity_paths_cannot_escape_the_trusted_root(catalog_root):
    _, plan = reviewed_sample()
    path = catalog_root / "catalogs" / "ISSUE_CATALOG.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["issues"][0]["implementation"] = "../unreviewed"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="version ownership"):
        load_report_context(catalog_root, allowed_units=plan)


def test_only_selected_catalog_fields_are_rendered_and_markup_is_escaped(catalog_root):
    result, plan = reviewed_sample()
    path = catalog_root / "catalogs" / "ISSUE_CATALOG.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["issues"][0]["title"] = "Reviewed [bracket] | <sample> *emphasis*"
    document["issues"][0]["private_diagnostic"] = "synthetic-unapproved-field"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    context = load_report_context(catalog_root, allowed_units=plan)
    markdown = render_markdown(result, allowed_units=plan, report_context=context)
    html = render_html(result, allowed_units=plan, report_context=context)
    assert r"\[bracket\] \| &lt;sample&gt; \*emphasis\*" in markdown
    assert "<sample>" not in html
    assert "Reviewed [bracket] | &lt;sample&gt; *emphasis*" in html
    assert "synthetic-unapproved-field" not in markdown + html


def test_reviewed_context_does_not_approve_arbitrary_model_summary(catalog_root):
    _, plan = reviewed_sample()
    result = aggregate_results(plan, (
        UnitResult(plan[0].unit_id),
        UnitResult(plan[1].unit_id, summary="synthetic private model prose"),
    ))
    with pytest.raises(PrivacyError):
        render_html(result, allowed_units=plan, report_context=load_report_context(
            catalog_root, allowed_units=plan,
        ))


def test_classified_card_aliases_explain_duplicate_noise_unexpected_and_history(catalog_root):
    _, plan = reviewed_sample()
    issue = plan[1]
    cards = (
        CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001"),
        CardVerdict("card-0002", CoreVerdict.CORRECT, "issue-001"),
        CardVerdict("card-0003", CoreVerdict.INCORRECT),
        CardVerdict("card-0004", CoreVerdict.CORRECT, "root-0001"),
        CardVerdict("card-0005", CoreVerdict.UNKNOWN, contribution=Contribution.HISTORICAL),
    )
    result = aggregate_results(plan, (UnitResult(plan[0].unit_id), UnitResult(issue.unit_id, cards)))
    html = unescape(re.sub("<[^>]+>", "", render_html(
        result, allowed_units=plan, report_context=load_report_context(catalog_root, allowed_units=plan),
    )))
    for index, classification in enumerate(
        ("expected_detection", "duplicate", "noise", "unexpected_real", "historical"), 1,
    ):
        assert f"card-{index:04d}: {classification}" in html
    assert "Same independently supported root as card-0001" in html
    assert "not Noise and earns no expected-issue credit" in html
    assert "Historical context only; no current score contribution" in html
    assert result.counts.to_dict() == {
        "correct_issues": 1, "expected_issues": 1, "noise_cards": 1, "duplicate_cards": 1,
    }


@pytest.mark.parametrize("excluded", [0, 1, 2])
def test_twenty_issue_coverage_stays_identical_with_reviewed_context(catalog_root, excluded):
    targets = select_daily(load_catalog(catalog_root), date(2026, 9, 4))
    plan = tuple(PlannedUnit(
        target.unit_id, None if target.is_baseline else target.unit_id.logical_version,
    ) for target in targets)
    actual = tuple(UnitResult(
        unit.unit_id, exclusion_reasons=(ExclusionReason.INCOMPLETE_EVIDENCE,)
        if index < excluded else (),
    ) for index, unit in enumerate(plan))
    result = aggregate_results(plan, actual)
    context = load_report_context(catalog_root, allowed_units=plan)
    plain_json = render_json(result, allowed_units=plan)
    html = render_html(result, allowed_units=plan, report_context=context)
    markdown = render_markdown(result, allowed_units=plan, report_context=context)
    for rendered in (html, markdown):
        assert f"{result.coverage.scored_issues}/20 planned" in rendered
        assert f"{result.coverage.scored_baselines}/5 planned" in rendered
        assert f"excluded whole units: {excluded}" in rendered
        if excluded:
            assert "not a confirmed Engine miss" in rendered
    assert render_json(result, allowed_units=plan) == plain_json
    assert json.loads(plain_json) == result.to_dict()


@pytest.mark.parametrize("field,value", [
    ("report_date", "2026-09-31"),
    ("report_date", "20260904"),
    ("region_display", ""),
    ("region_display", "https://private.example.test"),
    ("region_display", None),
    ("source_revision", "main"),
    ("source_revision", "f" * 39),
    ("source_revision", "F" * 40),
])
def test_metadata_is_explicit_and_validated_without_inferred_region_or_source(field, value):
    fields = dict(report_date="2026-09-04", region_display="Sweden Central", source_revision="f" * 40)
    fields[field] = value
    with pytest.raises(ReportContextError, match="metadata_invalid"):
        ReportMetadata(**fields)


def test_omitted_metadata_has_no_default_region_or_source():
    result, plan = reviewed_sample()
    html = render_html(result, allowed_units=plan)
    assert "Region:" not in html and "Source commit:" not in html
    metadata = ReportMetadata("2026-09-04", "SwedenCentral", "a" * 40)
    assert metadata.to_private_dict() == {
        "report_date": "2026-09-04", "region_display": "SwedenCentral", "source_revision": "a" * 40,
    }
