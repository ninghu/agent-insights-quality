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
from agent_insights_quality.reporting import (
    markdown_view, render_email_html, render_html, render_json, render_markdown,
)
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
        assert not re.search(r"\b(Full|Partial)\b", rendered)
        if rendered == markdown:
            assert "Quality score:" in rendered
        assert f"Matched: {result.counts.correct_issues}/{result.counts.expected_issues} scored issues" in rendered
        assert f"Noise: {result.counts.noise_cards}; Duplicate: {result.counts.duplicate_cards}" in rendered
        assert f"{result.coverage.scored_issues}/20 expected" in rendered
        assert f"{result.coverage.scored_baselines}/5 expected" in rendered
        assert f"{excluded} excluded units" in rendered
        if excluded:
            assert "Evidence incomplete" in rendered
            assert "Unconfirmed" in rendered and "1. Noise" in rendered
            assert "Unconfirmed rows are excluded from the score, not counted as misses" in rendered
        assert "Generated insight(s)" in rendered
        assert "Only new or updated findings are shown" in rendered
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
        assert expected["root_cause"] not in rendered
        assert expected["expected_fix"] not in rendered
        assert "traffic.json" not in rendered
        assert "1. card-0001" in rendered and "1. Noise" in rendered
        assert "Missed" in rendered
        assert "Core claim judged incorrect" not in rendered
        assert "one version run (10 attempts)" in rendered
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
    for index in range(1, 5):
        assert f"{index}. card-{index:04d}" in html
    for index, verdict in ((1, "Matched"), (2, "Duplicate"), (3, "Noise"), (4, "Unexpected")):
        assert f"{index}. {verdict}" in html
    assert "card-0005" not in html
    assert "Same root as finding 1" in html
    assert "saved valid non-target finding, not a match" in html
    assert "Correct (other)" not in html
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
        assert f"{result.coverage.scored_issues}/20 expected" in rendered
        assert f"{result.coverage.scored_baselines}/5 expected" in rendered
        assert f"{excluded} excluded units" in rendered
        if excluded:
            assert "Unconfirmed rows are excluded from the score, not counted as misses" in rendered
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


@pytest.mark.parametrize("excluded", [0, 1, 2, 3])
def test_email_brief_preserves_counts_and_numeric_coverage_without_verdict_labels(excluded):
    result, plan = report(excluded)
    html = render_email_html(result, allowed_units=plan, test_run=True)
    plain = unescape(re.sub("<[^>]+>", "", html))
    assert not re.search(r"\b(Full|Partial|PASS|FAIL)\b", plain)
    assert "max-width:960px" in html and "font-size:14px" in html
    assert "<!--[if mso]>" in html and 'role="presentation"' in html
    assert "TEST RUN" in plain
    assert "20 expected" in plain
    assert f'{result.counts.correct_issues} detected' in plain
    assert f'{result.counts.noise_cards} Noise; {result.counts.duplicate_cards} Duplicate' in plain
    assert "Baseline coverage" not in plain
    assert "Measurement exclusions" not in plain
    assert "E_scored=" not in plain and "C=" not in plain
    assert ("PERSONAL NOTICE" in plain) == (excluded > 2)
    assert "Score change" not in plain
    assert "Extra cards" not in plain
    assert "How Scoring Works" in plain
    assert 'id="how-scoring-works"' not in html
    assert html.index(">Summary<") < html.index(">What needs improvement<")
    assert html.index(">What needs improvement<") < html.index(">What is working<")
    assert html.index(">What is working<") < html.index(">Test Agents<")
    assert "Prompt</small>" not in html  # No inferred catalog type for synthetic identities.
    if excluded:
        assert f"{excluded} unit(s) excluded" in plain
        assert "evidence is not complete" in plain and "Human validation" in plain
    else:
        assert "excluded from every score count" not in plain


def test_email_brief_has_five_agent_rows_and_complete_detail_targets():
    result, plan = report()
    html = render_email_html(result, allowed_units=plan, details_href="report.html")
    agent_section = html.split(">Test Agents</h2>", 1)[1].split("</tbody>", 1)[0]
    assert agent_section.count('<tr bgcolor="#ffffff">') == 3
    assert agent_section.count('<tr bgcolor="#f8fafc">') == 2
    details = render_html(result, allowed_units=plan)
    links = re.findall(r'href="report\.html#([^"]+)"', html)
    assert len(set(links)) == 5
    for anchor in links:
        assert f'id="{anchor}"' in details
    assert "Reviewed contracts and follow-up" not in html
    assert "Scoring rules" not in html  # No invented published link.
    assert "Link pending publication" in html


def test_real_baseline_findings_are_not_automatically_noise_or_health_failures():
    baseline = PlannedUnit(UnitId("weather-agent", "v0"))
    issue = PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001")
    plan = (baseline, issue)
    result = aggregate_results(plan, (
        UnitResult(baseline.unit_id, (CardVerdict("card-0001", CoreVerdict.CORRECT, "root-0001"),)),
        UnitResult(issue.unit_id, (CardVerdict("card-0002", CoreVerdict.CORRECT, "issue-001"),)),
    ))
    html = render_email_html(result, allowed_units=plan)
    assert "1 of 1 scorable baselines had no confirmed Noise" in html
    assert "not a claim of perfect Agent health" in html
    assert "Independently supported Agent problem outside the expected defect" not in html
    assert "Other findings" not in html
    markdown = render_markdown(result, allowed_units=plan)
    assert "1. Unexpected" in markdown
    assert "saved valid non-target finding, not a match" in markdown
    assert "Healthy Agent versions should produce zero findings" not in html
    assert "Incorrect findings (Noise)</td>" not in html


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "https://example.test/private", "file:///private.html",
    "../report.html", 'report.html" onclick="x', "",
])
def test_brief_cannot_introduce_unvalidated_report_links(href):
    result, plan = report()
    with pytest.raises(ReportContextError, match="detail_link_invalid"):
        render_email_html(result, allowed_units=plan, details_href=href)


def test_agent_table_has_only_approved_columns_and_catalog_assignments(catalog_root):
    result, plan = reviewed_sample()
    before = load_report_context(catalog_root, allowed_units=plan)
    path = catalog_root / "catalogs" / "AGENT_CATALOG.yaml"
    catalog = yaml.safe_load(path.read_text())
    catalog["agents"][0]["owner"] = "Synthetic Reviewed Owner"
    path.write_text(yaml.safe_dump(catalog), encoding="utf-8")
    after = load_report_context(catalog_root, allowed_units=plan)
    assert before.to_private_dict()["units"] == after.to_private_dict()["units"]
    assert after.assignments["weather-agent"] == "Synthetic Reviewed Owner"
    html = render_email_html(
        result, allowed_units=plan, report_context=after, details_href="report.html",
    )
    table = html.split(">Test Agents</h2>", 1)[1]
    assert re.findall(r'<th[^>]*>([^<]+)</th>', table) == [
        "Agent", "Findings", "Human Validation", "Assigned To",
    ]
    assert "Synthetic Reviewed Owner" in table
    assert "report.html#weather-agent" in table
    assert not re.search(r"\bplanned\b", html, re.I)
    assert "Baseline coverage" not in html and "Run notes" not in html
    assert "How Scoring Works</h2>" not in html and "Run reference" not in html


@pytest.mark.parametrize("owner", [None, "", ["One", "Two"], "One;Two", "One, Two", "<script>One</script>"])
def test_catalog_owner_must_be_one_reviewed_person(catalog_root, owner):
    _, plan = reviewed_sample()
    path = catalog_root / "catalogs" / "AGENT_CATALOG.yaml"
    catalog = yaml.safe_load(path.read_text())
    catalog["agents"][0]["owner"] = owner
    path.write_text(yaml.safe_dump(catalog), encoding="utf-8")
    with pytest.raises(ReportContextError):
        load_report_context(catalog_root, allowed_units=plan)


def test_healthy_units_do_not_create_detailed_follow_up_boilerplate(catalog_root):
    _, plan = reviewed_sample()
    result = aggregate_results(plan, (
        UnitResult(plan[0].unit_id),
        UnitResult(plan[1].unit_id, (CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001"),)),
    ))
    markdown = render_markdown(
        result, allowed_units=plan, report_context=load_report_context(catalog_root, allowed_units=plan),
    )
    assert "No unexpected finding" not in markdown
    assert "Expected defect detected" not in markdown
    assert all(row.endswith(" | - |") for row in markdown.splitlines() if re.match(r"^\| [12] \|", row))
    assert "<details>" not in markdown
    assert "| 1. Matched | - |" in markdown
    assert len(re.findall(r"^\| [12] \|", markdown, re.M)) == 2
    assert "### weather-agent / v0" not in markdown
    assert "### weather-agent / issue-001" not in markdown


def test_compact_detail_has_five_tables_and_five_ordered_version_rows_each():
    result, plan = report(excluded=2)
    markdown = render_markdown(result, allowed_units=plan)
    heading = "| Run num | Agent version | Expected insight | Generated insight(s) | Assessment | Notes |"
    assert markdown.count(heading) == 5
    assert len(re.findall(r"^\| [1-5] \|", markdown, re.M)) == 25
    for section in markdown.split(heading)[1:]:
        rows = re.findall(r"^\| ([1-5]) \| (.*)$", section.split("\n## ")[0], re.M)
        assert [number for number, _ in rows] == ["1", "2", "3", "4", "5"]
        assert rows[0][1].startswith("v0 (deployment not recorded)")
    assert not re.search(r"^#{3,}", markdown, re.M)
    html = markdown_view(markdown)
    assert html.count("<thead>") == 5
    for section in html.split("<tbody>")[1:]:
        assert section.split("</tbody>")[0].count("<tr ") == 5
    assert "Scope commit" not in markdown
    assert len(markdown) < 6000


def test_markdown_browser_table_preserves_escaped_pipes_and_literal_break_tags(catalog_root):
    result, plan = reviewed_sample()
    path = catalog_root / "catalogs" / "ISSUE_CATALOG.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["issues"][0]["title"] = r"Two | cases \ with <br> and <script>tags</script>"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    markdown = render_markdown(
        result, allowed_units=plan,
        report_context=load_report_context(catalog_root, allowed_units=plan),
    )
    assert r"Two \| cases \\" in markdown
    html = markdown_view(markdown)
    rows = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert rows.count("<td ") == 12
    assert "Two | cases" in html
    assert "&lt;br&gt;" in html and "<script>" not in html
    assert "<br>" not in html.split("Two | cases", 1)[1].split("</td>", 1)[0]


@pytest.mark.parametrize("markup", [
    '<details open><summary>Assessment details</summary>text</details>',
    '<details><summary onclick="alert(1)">Assessment details</summary>text</details>',
    '<details><summary>Other summary</summary>text</details>',
    '<details><summary>Assessment details</summary><br><strong>Finding 1: Saved assessment</strong>'
    '<br><script>alert(1)</script></details>',
    '<details><summary>Assessment details</summary><br><strong onclick="alert(1)">'
    'Finding 1: Saved assessment</strong><br>text</details>',
    '<a href="javascript:alert(1)">link</a><img src="https://invalid.test/x">',
])
def test_browser_only_recognizes_strict_generated_details_markup(markup):
    html = markdown_view(markup)
    assert "<details" not in html and "<summary" not in html
    assert "<script" not in html and "<img" not in html
    assert 'href="javascript:' not in html and 'src="https:' not in html
    assert '<strong onclick=' not in html


def test_browser_does_not_interpret_untrusted_bold_or_html_entities_as_markup():
    html = markdown_view(
        r"\*\*Assigned To:\*\* \*\*model label\*\* &lt;details&gt;"
        "&lt;summary&gt;Assessment details&lt;/summary&gt;&lt;/details&gt;\n"
        "**Assigned To:** Synthetic Owner"
    )
    assert "<details>" not in html and "<summary>" not in html
    assert "**Assigned To:** **model label**" in html
    assert "<strong>Assigned To:</strong> Synthetic Owner" in html
    assert html.count("<strong>") == 1


def test_browser_table_separator_distinguishes_odd_and_even_backslashes():
    markdown = (
        "| First | Second |\n| --- | --- |\n"
        r"| Slash \\| Other |" "\n"
        r"| Literal \\\| pipe | Last |" "\n"
    )
    html = markdown_view(markdown)
    rows = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert rows.count("<td ") == 4
    assert r"Slash \</td>" in html
    assert r"Literal \| pipe" in html
