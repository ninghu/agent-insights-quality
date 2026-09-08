from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

from agent_insights_quality.report_context import ReportContextError, ReportMetadata, load_report_context
from agent_insights_quality.report_review import RetainedReviewContext
from agent_insights_quality.reporting import (
    markdown_view, render_email_html, render_markdown, render_private_markdown,
)
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, ExclusionReason, PlannedUnit, UnitId, UnitResult, aggregate_results,
)
from agent_insights_quality.state import RuntimeStore


ROOT = Path(__file__).resolve().parents[2]


def seed_review(runtime, *, corrupt=False):
    plan = (
        PlannedUnit(UnitId("healthcare-agent", "v0")),
        PlannedUnit(UnitId("support-ticket-agent", "v0")),
        PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001"),
    )
    result = aggregate_results(plan, (
        UnitResult(plan[0].unit_id, (CardVerdict("card-0001", CoreVerdict.CORRECT, "root-0001"),)),
        UnitResult(plan[1].unit_id, (CardVerdict("card-0001", CoreVerdict.CORRECT, "root-0001"),)),
        UnitResult(plan[2].unit_id),
    ))
    records = runtime.run("synthetic-review")
    with runtime.ownership():
        for index, unit in enumerate(result.units):
            identity = unit.planned.unit_id
            key = f"targets/{identity.agent}/{identity.logical_version}"
            artifact = key + "/assessments/synthetic"
            records.save_completed(key + "/source", {
                "assessment": {"run_id": "synthetic-review", "artifact": artifact},
                "traffic_run_id": "synthetic-review", "work_key": key + "/work-synthetic",
                "tested_at": "2026-09-05T08:00:00Z", "assessed_at": "2026-09-05T08:10:00Z",
                "traffic_source_revision": "a" * 40, "source_revision": "b" * 40,
            })
            records.save_completed(key + "/work-synthetic/deployment", {
                "target_key": f"{identity.agent}/{identity.logical_version}",
                "agent_name": identity.agent, "provider_version": str(11 + index),
                "source_revision": "a" * 40,
            })
            citations = [{"attempt": 1, "step_id": "probe-01", "refs": ["endpoint-01"]}]
            cards = [{
                "card_alias": "card-0001", "core": "correct", "expected_match": False,
                "root_group": "synthetic-root", "citations": citations,
                "severity": "unknown", "proposed_fix": "unknown",
                "reason": (
                    "PRIVATE_ONLY_MARKER independent endpoint evidence supports this label."
                    if index == 0 else
                    "PRIVATE_ONLY_MARKER proposed fix is unnecessary because the agent already performs graceful degradation."
                ),
            }] if index < 2 else []
            saved = {
                "unit_id": identity.to_dict(), "cards": [
                    {k: v for k, v in f.to_dict().items() if k not in {"classification", "scored"}}
                    for f in unit.findings
                ],
                "summary": unit.summary, "exclusion_reasons": [],
            }
            if corrupt and index == 0:
                saved["cards"][0]["root_cause_alias"] = "root-9999"
            payload = {
                "target": {"unit_id": identity.to_dict()},
                "attempts": [{"index": 1, "steps": [{
                    "step_id": "probe-01", "allowed_citation_refs": ["endpoint-01"],
                    "endpoint_ref": "endpoint-01",
                    "execution": {"response": {"output_text": "Available appointment record: slot-demo-a8"}},
                }]}],
                "cards": [{
                    "card_alias": "card-0001", "current": {
                        "title": ("Unsupported availability label on existing appointment" if index == 0
                                  else "Graceful degradation on optional dependency failure"),
                        "description": "<script>PRIVATE_ONLY_MARKER</script> [unsafe](javascript:alert(1))",
                    },
                }] if index < 2 else [],
            }
            records.save_artifact(artifact, {
                "unit_result": saved, "private_detail": {
                    "input": payload, "resolved": {
                        "attempts": [{
                            "index": number, "sufficient": number == 1, "observed": number == 1,
                            "reason": "Synthetic observed defect", "citations": citations if number == 1 else [],
                        } for number in range(1, 11)],
                        "cards": cards, "limitations": [],
                    },
                },
            })
    return result, plan


def test_private_review_quotes_evidence_and_distinguishes_benign_from_confirmation(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    before = deepcopy(result.to_dict())
    review = RetainedReviewContext(runtime, "synthetic-review", result)
    context = load_report_context(ROOT, allowed_units=plan)
    markdown = render_private_markdown(
        result, allowed_units=plan, review_context=review, report_context=context,
    )
    assert "automatic Agent-fix recommendations" in markdown
    assert "proposed fix is unnecessary" in markdown
    assert "slot-demo-a8" not in markdown
    assert "Unsupported availability label on existing appointment" in markdown
    assert "PRIVATE_ONLY_MARKER" in markdown
    assert "endpoint-01" not in markdown and "probe-01" not in markdown
    assert "Generated insight(s)" in markdown
    assert "runs/synthetic-review/artifacts/" not in markdown
    assert "1. Unexpected" in markdown and "Correct (other)" not in markdown
    assert "11 (v0)" in markdown and "12 (v0)" in markdown and "13 (issue-001)" in markdown
    assert "<script>" not in markdown_view(markdown)
    assert 'href="javascript:' not in markdown_view(markdown)
    public = render_markdown(result, allowed_units=plan, report_context=context)
    email = render_email_html(result, allowed_units=plan, report_context=context)
    assert "PRIVATE_ONLY_MARKER" not in public + email
    assert "slot-demo-a8" not in public + email
    assert result.to_dict() == before
    assert result.counts.noise_cards == 0 and result.counts.correct_issues == 0
    assert result.score == 0.0


def test_private_context_cannot_be_injected_at_public_boundary(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    with pytest.raises(TypeError):
        render_markdown(result, allowed_units=plan, private_context={"secret": "synthetic"})
    with pytest.raises(ReportContextError, match="review_context_invalid"):
        render_private_markdown(result, allowed_units=plan, review_context={})


def test_private_overall_and_agent_views_link_the_issue_not_the_provider_version(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    before = deepcopy(result.to_dict())
    review = RetainedReviewContext(runtime, "synthetic-review", result)
    context = load_report_context(ROOT, allowed_units=plan)
    metadata = ReportMetadata("2026-09-05", "Sweden Central", "b" * 40)
    href = f"https://github.com/ninghu/agent-insights-quality/blob/{metadata.source_revision}/ISSUE_CATALOG.md#issue-001"
    definition = context.for_plan(plan)[plan[2].unit_id].expected_symptom
    for agent in (None, "weather-agent"):
        markdown = render_private_markdown(
            result, allowed_units=plan, review_context=review, report_context=context,
            metadata=metadata, agent=agent,
        )
        assert f"13 ([issue-001]({href}))" in markdown
        assert f'13 (<a href="{href}">issue-001</a>)' in markdown_view(markdown)
        issue_row = next(row for row in markdown.splitlines() if f"13 ([issue-001]({href}))" in row)
        assert issue_row.strip("| ").split(" | ")[2] == definition
        assert "| Issue definition | Expected insight |" in markdown and "| Notes |" not in markdown
        assert "#v0" not in markdown
    assert result.to_dict() == before


def test_retained_judgment_cannot_substitute_a_different_frozen_result(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    result, _ = seed_review(runtime, corrupt=True)
    with pytest.raises(ReportContextError, match="review_result_mismatch"):
        RetainedReviewContext(runtime, "synthetic-review", result)


def test_derived_unknown_core_exclusion_does_not_block_private_failure_details(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    planned = PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001")
    original = UnitResult(
        planned.unit_id, (CardVerdict("card-0001", CoreVerdict.UNKNOWN),),
        (ExclusionReason.INCOMPLETE_ASSESSMENT,),
    )
    result = aggregate_results((planned,), (original,))
    records = runtime.run("synthetic-incomplete")
    key = "targets/weather-agent/issue-001"
    with runtime.ownership():
        records.save_completed(key + "/source", {
            "assessment": {"run_id": "synthetic-incomplete", "artifact": key + "/assessment"},
        })
        records.save_artifact(key + "/assessment", {
            "unit_result": {
                "unit_id": planned.unit_id.to_dict(), "summary": None,
                "cards": [{
                    "card_alias": "card-0001", "core": "unknown", "root_cause_alias": None,
                    "contribution": "current", "severity": "unknown", "proposed_fix": "unknown",
                    "summary": None,
                }],
                "exclusion_reasons": ["incomplete_assessment"],
            },
            "private_detail": {"input": {
                "target": {"unit_id": planned.unit_id.to_dict()}, "attempts": [], "cards": [],
            }, "resolved": None},
        })
    before = result.to_dict()
    context = RetainedReviewContext(runtime, "synthetic-incomplete", result)
    markdown = render_private_markdown(
        result, allowed_units=(planned,), review_context=context,
    )
    assert "Assessment incomplete" in markdown and "Finding unconfirmed" in markdown
    assert "Unconfirmed" in markdown
    assert result.to_dict() == before


def test_human_triage_is_not_selected_by_specific_agent_or_card_title(tmp_path, monkeypatch):
    from agent_insights_quality.state import RecordStore
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    read = RecordStore.read_artifact
    def renamed(self, *args, **kwargs):
        value = read(self, *args, **kwargs)
        for card in value.get("private_detail", {}).get("input", {}).get("cards", []):
            card["current"]["title"] = "A different correctly handled situation"
        return value
    monkeypatch.setattr(RecordStore, "read_artifact", renamed)
    context = RetainedReviewContext(runtime, "synthetic-review", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert "automatic Agent-fix recommendations" in markdown
    assert "saved valid non-target finding, not a match" in markdown
    assert "A different correctly handled situation" in markdown


def test_readiness_exclusion_retains_actual_deployment_without_claiming_no_findings(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    planned = PlannedUnit(UnitId("weather-agent", "issue-003"), "issue-003")
    result = aggregate_results((planned,), (
        UnitResult(planned.unit_id, exclusion_reasons=(ExclusionReason.INCOMPLETE_EVIDENCE,)),
    ))
    records = runtime.run("synthetic-readiness")
    key = "targets/weather-agent/issue-003"
    with runtime.ownership():
        records.save_completed(key + "/source", {
            "traffic_run_id": "synthetic-readiness", "work_key": key + "/work-one",
        })
        records.save_completed(key + "/work-one/deployment", {
            "target_key": "weather-agent/issue-003", "agent_name": "actual-weather-object",
            "provider_version": "27", "source_revision": "a" * 40,
        })
        records.save_progress(key + "/failure", {
            "code": "trace_readiness_insufficient", "stage": "evidence",
        })
    context = RetainedReviewContext(runtime, "synthetic-readiness", result)
    markdown = render_private_markdown(result, allowed_units=(planned,), review_context=context)
    assert "27 (issue-003)" in markdown
    assert "Not generated (trace readiness insufficient)" in markdown
    assert "| Unconfirmed<br>Evidence incomplete." in markdown
    assert "Unconfirmed rows are excluded from the score, not counted as misses" in markdown
    assert "actual-weather-object" not in markdown
    assert "Expected defect: Missed" not in markdown


def test_full_reasons_are_retained_only_in_native_collapsed_details(tmp_path, monkeypatch):
    from agent_insights_quality.state import RecordStore
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    read = RecordStore.read_artifact
    def long_reason(self, *args, **kwargs):
        value = read(self, *args, **kwargs)
        for card in value.get("private_detail", {}).get("resolved", {}).get("cards", []):
            card["reason"] = "Observed successful fallback. " * 30 + "FINAL_REASON_END"
        return value
    monkeypatch.setattr(RecordStore, "read_artifact", long_reason)
    context = RetainedReviewContext(runtime, "synthetic-review", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert markdown.count("Observed successful fallback.") == 60
    assert markdown.count("FINAL_REASON_END") == 2
    assert "..." not in markdown
    assert "shortened saved judgments" not in markdown
    html = markdown_view(markdown)
    assert html.count("<details><summary>Assessment details</summary>") == 2
    assert "Observed successful fallback" not in re.sub(r"<details>.*?</details>", "", html)
    assert html.count("FINAL_REASON_END") == 2
    parser = Markup()
    parser.feed(html)
    assert [attrs for tag, attrs in parser.tags if tag == "details"] == [{}, {}]
    assert sum(tag == "summary" for tag, _ in parser.tags) == 2
    assert "javascript:alert" not in markdown
    assert "response_output" not in markdown and "source_revision" not in markdown


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "br":
            self.text.append("\n")

    def handle_data(self, data):
        self.text.append(data)


def judgment(alias="card-0001", *, core="correct", root="other-root", expected=False, reason="Saved reason"):
    return {
        "card_alias": alias, "core": core, "root_group": root, "expected_match": expected,
        "severity": "unknown", "proposed_fix": "unknown", "reason": reason,
        "citations": [{"attempt": 1, "step_id": "probe-01", "refs": ["endpoint-01", "row-01"]}],
    }


def seed_passes(runtime, first, second):
    from agent_insights_quality.assessment import _merge_review

    planned = PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001")
    canonical = [{
        "card_alias": card["card_alias"], "contribution": "current",
        "current": {"title": "Synthetic finding", "description": "Private claim"},
    } for card in first]
    attempts = [{
        "index": number, "sufficient": number == 1, "observed": number == 1,
        "reason": "Saved observation", "citations": first[0]["citations"] if number == 1 else [],
    } for number in range(1, 11)]
    initial = {"cards": first, "attempts": attempts, "limitations": []}
    review = {"cards": second, "attempts": attempts, "limitations": []}
    resolved, disagreement = _merge_review(initial, review, canonical)
    roots = {
        group: f"root-{index:04d}" for index, group in enumerate(sorted({
            card["root_group"] for card in resolved["cards"]
            if card["core"] == "correct" and not card["expected_match"]
        }), 1)
    }
    actual = UnitResult(planned.unit_id, tuple(CardVerdict(
        card["card_alias"], CoreVerdict(card["core"]),
        ("issue-001" if card["expected_match"] else roots[card["root_group"]])
        if card["core"] == "correct" else None,
    ) for card in resolved["cards"]), (ExclusionReason.INCOMPLETE_ASSESSMENT,) if disagreement else ())
    result = aggregate_results((planned,), (actual,))
    unit = result.units[0]
    records = runtime.run("synthetic-passes")
    with runtime.ownership():
        records.save_completed("targets/weather-agent/issue-001/source", {
            "assessment": {"run_id": "synthetic-passes", "artifact": "assessment"},
        })
        records.save_artifact("assessment", {
            "unit_result": {
                "unit_id": planned.unit_id.to_dict(), "summary": unit.summary,
                "exclusion_reasons": [reason.value for reason in actual.exclusion_reasons],
                "cards": [
                    {k: v for k, v in finding.to_dict().items() if k not in {"classification", "scored"}}
                    for finding in unit.findings
                ],
            },
            "private_detail": {
                "input": {
                    "target": {"unit_id": planned.unit_id.to_dict()},
                    "attempts": [{"index": 1, "steps": [{
                        "step_id": "probe-01", "allowed_citation_refs": ["endpoint-01", "row-01"],
                    }]}],
                    "cards": canonical,
                },
                "initial": initial, "review": review, "resolved": resolved,
            },
        })
    return result, (planned,)


def test_disagreement_explains_both_full_passes_without_rejudgment_or_archive_changes(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    initial_reason = "Initial causal uncertainty. " * 20 + "INITIAL_END"
    review_reason = "Positive reviewed interpretation. " * 20 + "REVIEW_END"
    result, plan = seed_passes(
        runtime, [judgment(core="incorrect", root=None, reason=initial_reason)],
        [judgment(reason=review_reason)],
    )
    before = deepcopy(result.to_dict())
    files = {path: path.read_bytes() for path in runtime.directory.rglob("*.json")}
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    html = markdown_view(markdown)
    assert "Review disagreement." in markdown
    assert "Initial assessment (incorrect)" in markdown and "Focused review (correct)" in markdown
    assert initial_reason in markdown and review_reason in markdown
    assert "Resolved assessment" not in markdown  # The identical review prose is not repeated.
    assert initial_reason not in re.sub(r"<details>.*?</details>", "", html)
    assert review_reason not in re.sub(r"<details>.*?</details>", "", html)
    assert "1. Unconfirmed" in markdown
    row = next(line for line in markdown.splitlines() if line.startswith("| 1 |"))
    assessment_cell = row.strip("| ").split(" | ")[-1]
    assert assessment_cell.startswith("1. Unconfirmed<br>")
    assert "Review disagreement." in assessment_cell and "<details>" in assessment_cell
    assert "Core: unknown; classification: unknown." in html
    assert result.counts.expected_issues == result.counts.noise_cards == 0
    assert not result.units[0].scorable
    assert {reason.value for reason in result.units[0].exclusion_reasons} == {
        "incomplete_assessment", "unknown_core",
    }
    assert result.to_dict() == before
    assert {path: path.read_bytes() for path in runtime.directory.rglob("*.json")} == files
    assert context.provenance()["weather-agent/issue-001"]["sha256"]
    assert "INITIAL_END" not in render_markdown(result, allowed_units=plan)
    assert "REVIEW_END" not in render_email_html(result, allowed_units=plan)


@pytest.mark.parametrize("change", ["expected", "partition", "rename"])
def test_disagreement_uses_semantic_expected_match_and_root_partition_not_labels(tmp_path, change):
    runtime = RuntimeStore("daily", root=tmp_path)
    initial = [judgment(), judgment("card-0002")]
    review = deepcopy(initial)
    if change == "expected":
        review[0]["expected_match"] = True
        review[0]["root_group"] = "expected-root"
    elif change == "partition":
        review[1]["root_group"] = "separate-root"
    else:
        for card in review:
            card["root_group"] = "arbitrarily-renamed-root"
    result, plan = seed_passes(runtime, initial, review)
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert ("Review disagreement" in markdown) == (change != "rename")
    assert ("Initial assessment" in markdown) == (change != "rename")
    assert result.units[0].scorable == (change == "rename")


@pytest.mark.parametrize("stage", ["initial", "review", "resolved"])
@pytest.mark.parametrize("corruption", ["schema", "alias", "missing", "duplicate", "citation", "attempt-citation"])
def test_all_quoted_passes_validate_schema_membership_and_citations(tmp_path, monkeypatch, stage, corruption):
    from agent_insights_quality.state import RecordStore

    runtime = RuntimeStore("daily", root=tmp_path)
    result, _ = seed_passes(runtime, [judgment(core="incorrect", root=None)], [judgment()])
    read = RecordStore.read_artifact
    def corrupted(self, *args, **kwargs):
        artifact = read(self, *args, **kwargs)
        output = artifact["private_detail"][stage]
        if corruption == "schema":
            output["cards"][0]["reason"] = 4
        elif corruption == "alias":
            output["cards"][0]["card_alias"] = "other-card"
        elif corruption == "missing":
            output["cards"].clear()
        elif corruption == "duplicate":
            output["cards"].append(deepcopy(output["cards"][0]))
        else:
            item = output["cards"][0] if corruption == "citation" else output["attempts"][0]
            item["citations"][0]["refs"].append("unrelated-row")
        return artifact
    monkeypatch.setattr(RecordStore, "read_artifact", corrupted)
    with pytest.raises(ReportContextError):
        RetainedReviewContext(runtime, "synthetic-passes", result)


def test_full_rationales_escape_html_markdown_pipes_backslashes_and_newlines(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    reason = (
        'Literal <script>alert(1)</script> <img src="https://invalid.test/x" onerror="alert(1)">\n'
        r'Pipes \| and \\|, **bold**, [link](javascript:alert(1)), `code`'
        '\n</details><details open><summary>Assessment details</summary>'
        '<iframe src="https://invalid.test"></iframe>&lt;script&gt;\nLAST_REASON_LINE'
    )
    result, plan = seed_passes(
        runtime, [judgment(core="incorrect", root=None, reason=reason)], [judgment(reason=reason + "_REVIEW")],
    )
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    html = markdown_view(markdown)
    parsed = Markup()
    parsed.feed(html)
    assert reason in "".join(parsed.text)
    assert reason + "_REVIEW" in "".join(parsed.text)
    assert markdown.count("LAST_REASON_LINE") == 2
    assert "&#92;&#124;" in markdown and "&#92;&#92;&#124;" in markdown
    assert "[link](javascript:" not in markdown
    assert len(re.findall(r"^\| 1 \|", markdown, re.M)) == 1
    assert html.split("<tbody>", 1)[1].split("</tbody>", 1)[0].count("<td ") == 6
    assert [attrs for tag, attrs in parsed.tags if tag == "details"] == [{}]
    assert not {"script", "img", "iframe"} & {tag for tag, _ in parsed.tags}
    assert not any("href" in attrs or "src" in attrs or any(k.startswith("on") for k in attrs)
                   for _, attrs in parsed.tags)
    assert "LAST_REASON_LINE" not in re.sub(r"<details>.*?</details>", "", html)
    assert context.for_result(result)[plan[0].unit_id]["cards"]["card-0001"]["reason"] == reason + "_REVIEW"


def test_distinct_resolved_rationale_is_not_lost_when_both_passes_are_shown(tmp_path, monkeypatch):
    from agent_insights_quality.state import RecordStore

    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_passes(runtime, [judgment(core="incorrect", root=None)], [judgment()])
    read = RecordStore.read_artifact
    def changed_reason(self, *args, **kwargs):
        artifact = read(self, *args, **kwargs)
        artifact["private_detail"]["resolved"]["cards"][0]["reason"] = "A distinct resolved rationale END"
        return artifact
    monkeypatch.setattr(RecordStore, "read_artifact", changed_reason)
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert "Resolved assessment (unknown)" in markdown
    assert "A distinct resolved rationale END" in markdown


def test_normal_detection_has_no_notes_button_and_duplicate_only_references_its_root(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    cards = [
        judgment(expected=True, root="expected-root", reason="NORMAL_PRIVATE_REASON"),
        judgment("card-0002", expected=True, root="expected-root", reason="REPEATED_PRIVATE_REASON"),
    ]
    result, plan = seed_passes(runtime, cards, deepcopy(cards))
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert "2: Same root as finding 1." in markdown
    assert "<details>" not in markdown
    assert "NORMAL_PRIVATE_REASON" not in markdown and "REPEATED_PRIVATE_REASON" not in markdown
    assert "Expected defect detected" not in markdown
    assert result.counts.correct_issues == result.counts.duplicate_cards == 1


def test_private_miss_identifies_insights_and_preserves_observation_count(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    context = RetainedReviewContext(runtime, "synthetic-review", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    missed_row = next(row for row in markdown.splitlines() if "| 13 (issue-001) |" in row)
    assert "| Missed<br>Observed 1/10. |" in missed_row
    assert "Observed 1/10." in missed_row
    assert "Expected defect missed" not in missed_row and "<details>" not in missed_row


def test_confirmed_noise_keeps_its_count_and_full_private_rationale(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    cards = [judgment(core="incorrect", root=None, reason="NOISE_PROOF " * 40 + "NOISE_END")]
    result, plan = seed_passes(runtime, cards, deepcopy(cards))
    before = result.to_dict()
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    html = markdown_view(markdown)
    parsed = Markup()
    parsed.feed(html)
    assert "1. Noise" in markdown and "Review disagreement" not in markdown
    assert cards[0]["reason"] in "".join(parsed.text)
    assert "NOISE_END" not in re.sub(r"<details>.*?</details>", "", html)
    assert result.to_dict() == before and result.counts.noise_cards == 1


def test_legacy_unknown_without_passes_keeps_full_reason_with_unresolved_label(tmp_path, monkeypatch):
    from agent_insights_quality.state import RecordStore

    runtime = RuntimeStore("daily", root=tmp_path)
    reason = "Unresolved legacy rationale. " * 30 + "LEGACY_END"
    result, plan = seed_passes(runtime, [judgment(core="unknown", root=None, reason=reason)],
                              [judgment(core="unknown", root=None, reason=reason)])
    read = RecordStore.read_artifact
    def legacy(self, *args, **kwargs):
        artifact = read(self, *args, **kwargs)
        del artifact["private_detail"]["initial"]
        del artifact["private_detail"]["review"]
        return artifact
    monkeypatch.setattr(RecordStore, "read_artifact", legacy)
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert "Resolved assessment (unknown)" in markdown
    assert reason in markdown
    assert "Assessment disagreement" not in markdown
    assert not result.units[0].scorable


def test_unicode_line_separators_cannot_break_a_private_markdown_row(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    reason = "First\u2028Second\u2029Last"
    cards = [judgment(reason=reason)]
    result, plan = seed_passes(runtime, cards, deepcopy(cards))
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert "First&#8232;Second&#8233;Last" in markdown
    assert len([line for line in markdown.splitlines() if line.startswith("| 1 |")]) == 1
    parsed = Markup()
    parsed.feed(markdown_view(markdown))
    assert reason in "".join(parsed.text)


def test_expected_miss_precedes_non_target_card_without_changing_its_saved_judgment(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    cards = [judgment(reason="Saved evidence about a separate claim.")]
    result, plan = seed_passes(runtime, cards, deepcopy(cards))
    before = deepcopy(result.to_dict())
    context = RetainedReviewContext(runtime, "synthetic-passes", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    row = next(line for line in markdown.splitlines() if line.startswith("| 1 |"))
    assessment_cell = row.strip("| ").split(" | ")[-1]
    assert assessment_cell.startswith("Missed<br>1. Unexpected<br>Observed 1/10.")
    assert "<details>" in assessment_cell
    assert "Observed 1/10." in row
    assert "Generated finding concerns a different claim." not in row
    assert "Correct (other)" not in markdown and "1. Correct" not in row
    html = markdown_view(markdown)
    assert "Core: correct; classification: unexpected_real." in html
    assert "Core: correct" not in re.sub(r"<details>.*?</details>", "", html)
    assert "Saved evidence about a separate claim." in html
    assert context.for_result(result)[plan[0].unit_id]["cards"]["card-0001"]["core"] == "correct"
    assert result.to_dict() == before
    assert result.counts.to_dict() == {
        "correct_issues": 0, "expected_issues": 1, "noise_cards": 0, "duplicate_cards": 0,
    }


def test_baseline_rows_have_no_expected_issue_outcome(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    context = RetainedReviewContext(runtime, "synthetic-review", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    baseline_rows = [row for row in markdown.splitlines() if "(v0)" in row and row.startswith("| ")]
    assert len(baseline_rows) == 2
    assert all("Expected issue:" not in row for row in baseline_rows)
    assert all(row.strip("| ").split(" | ")[-1].startswith("1. Unexpected<br>") for row in baseline_rows)


def test_private_agent_heading_uses_verified_native_foundry_link_only(tmp_path):
    from test_report_links import seed_foundry
    from agent_insights_quality.report_links import foundry_links

    runtime = RuntimeStore("daily", root=tmp_path)
    plan = seed_foundry(runtime)
    result = aggregate_results(plan, tuple(UnitResult(unit.unit_id) for unit in plan))
    expected, _ = foundry_links(runtime, "synthetic-daily", plan)
    context = RetainedReviewContext(runtime, "synthetic-daily", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=context)
    assert f"## [Weather]({expected['weather-agent']})" in markdown
    html = markdown_view(markdown)
    assert f'<h2><a href="{expected["weather-agent"]}">Weather</a></h2>' in html
    assert "ai.azure.com" not in render_markdown(result, allowed_units=plan)
    assert "<ul>" in html and html.count("<li>") == 4
    assert html.index("</ul>") < html.index("<h2>")


def test_missing_foundry_identity_keeps_plain_heading_without_inventing_link(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    result, plan = seed_review(runtime)
    review = RetainedReviewContext(runtime, "synthetic-review", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=review)
    assert "## Healthcare" in markdown and "ai.azure.com" not in markdown
