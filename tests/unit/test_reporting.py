from dataclasses import replace
from html import unescape
import json
import re

import pytest

from agent_insights_quality.reporting import render_html, render_json, render_markdown
from agent_insights_quality.results import (
    CardVerdict,
    CoreVerdict,
    ExclusionReason,
    PlannedUnit,
    UnitId,
    UnitResult,
    aggregate_results,
)


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
