from copy import deepcopy
from pathlib import Path

import pytest

from agent_insights_quality.report_context import ReportContextError, load_report_context
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
    assert "Already handled / no Agent change requested" in markdown
    assert "Human confirmation needed, not a confirmed Agent fix task" in markdown
    assert "slot-demo-a8" in markdown
    assert "appointment record" in markdown
    assert "PRIVATE_ONLY_MARKER" in markdown
    assert "endpoint-01" in markdown and "probe-01" in markdown
    assert "Expected Engine behavior" in markdown
    assert "runs/synthetic-review/artifacts/" in markdown
    assert "Human confirmation needed" in markdown
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
    assert "incomplete_assessment" in markdown and "unknown_core" in markdown
    assert "Measurement exclusion" in markdown
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
    assert "Human confirmation needed, not a confirmed Agent fix task" in markdown
    assert "Already handled / no Agent change requested" in markdown
    assert "A different correctly handled situation" in markdown
