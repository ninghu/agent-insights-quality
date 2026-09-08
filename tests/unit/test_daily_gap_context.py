from copy import deepcopy
from dataclasses import replace

import pytest

from agent_insights_quality.results import ExclusionReason

import test_assessment as fake


@pytest.mark.parametrize("gap_in_visible_only", [False, True])
def test_retained_parent_gap_is_explained_without_changing_evidence_or_readiness(gap_in_visible_only):
    target, attempts, invocations, snapshot = fake.evidence()
    with_gap = replace(snapshot, gaps=("attributed_log_parent_missing",))
    current, visible = (snapshot, with_gap) if gap_in_visible_only else (with_gap, snapshot)
    before = deepcopy(current.to_private_dict()), deepcopy(visible.to_private_dict())
    sol = fake.Sol()
    assessed = fake.daily(
        (target, attempts, invocations, current), sol, visible_snapshot=visible,
    )
    facts = sol.calls[0]["measurement_facts"]
    assert facts["retained_log_parent_missing"] is True
    assert facts["attributable_probe_attempts"] == facts["pre_insights_attributable_probe_attempts"] == 10
    assert sol.calls[0]["snapshot"] == before[0]
    assert sol.calls[0]["visible_snapshot"] == before[1]
    assert "exact response/Agent/version ownership" in sol.instructions[0]
    description = sol.schemas[0]["properties"]["limitations"]["description"]
    assert "Explain the affected claim and missing proof" in description
    assert not assessed.unit_result.exclusion_reasons
    assert len(sol.calls) == 1


@pytest.mark.parametrize("remaining", ["unknown_core", "essential_gap", "incomplete_query"])
def test_retained_parent_gap_never_clears_independent_uncertainty(remaining):
    target, attempts, invocations, snapshot = fake.evidence()
    snapshot = replace(
        snapshot, gaps=("attributed_log_parent_missing",),
        query_complete=remaining != "incomplete_query",
    )

    def output(payload):
        value = fake.output(payload)
        if remaining == "unknown_core":
            value["cards"][0].update(
                core="unknown", root_group=None, expected_match=False,
                reason="An essential governing obligation is unresolved.", citations=[],
            )
        elif remaining == "essential_gap":
            value["limitations"] = ["incomplete_evidence"]
            value["cards"][0]["reason"] = "Independent proof needed for the causal claim is unavailable."
        return value

    sol = fake.Sol(output)
    assessed = fake.daily((target, attempts, invocations, snapshot), sol)
    expected = (
        ExclusionReason.UNKNOWN_CORE if remaining == "unknown_core"
        else ExclusionReason.INCOMPLETE_EVIDENCE
    )
    assert expected in assessed.unit_result.exclusion_reasons
    assert all(call["measurement_facts"]["retained_log_parent_missing"] for call in sol.calls)
    assert 1 <= len(sol.calls) <= 2
    assert fake.aggregate((target, attempts, invocations, snapshot), assessed).score is None
