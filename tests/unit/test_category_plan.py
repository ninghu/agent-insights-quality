"""Frozen category attribution is a run input, never a live dashboard lookup."""

from copy import deepcopy
from dataclasses import replace

import pytest

from agent_insights_quality.results import CATEGORY_POLICY
from agent_insights_quality.runner import freeze_daily_plan
from agent_insights_quality.settings import AssessmentSettings
from agent_insights_quality.state import CheckpointError, RecordStore, StateError
from test_runner import DAY, Harness, fake_storage


@pytest.fixture
def harness(tmp_path, monkeypatch):
    fake_storage(monkeypatch)
    return Harness(tmp_path)


def changed_categories(targets):
    return tuple(replace(
        target, expectation={**target.expectation, "category": "output_quality"},
    ) if not target.is_baseline else target for target in targets)


def test_plan_is_persisted_before_providers_and_not_reclassified_on_resume(harness):
    h = harness
    with h.store.ownership():
        records = h.store.run("trial")
        plan = freeze_daily_plan(records, h.catalog.targets, revision="source-one")
        original = records.read_completed("result-plan")
        assert plan[1].category == "hallucinations"
        assert original["units"] == [unit.to_dict() for unit in plan]
        assert not h.cloud.events and not h.sol.calls
        assert freeze_daily_plan(
            records, changed_categories(h.catalog.targets), revision="source-one",
        ) == plan
        assert records.read_completed("result-plan") == original
        with pytest.raises(StateError, match="result_plan_invalid"):
            freeze_daily_plan(records, h.catalog.targets, revision="source-two")


def test_runner_resume_uses_the_frozen_plan_without_repeating_completed_work(harness):
    h = harness
    result = h.daily()
    before = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)
    h.catalog = replace(h.catalog, targets=changed_categories(h.catalog.targets))
    assert h.daily() == result
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)) == before
    assert result.units[1].planned.category == "hallucinations"
    assert h.store.run("trial").read_completed("run")["category_policy"] == CATEGORY_POLICY


def test_existing_legacy_run_stays_uncategorized(harness):
    h = harness
    with h.store.ownership():
        records = h.store.run("trial")
        records.save_completed("run", {
            "kind": "daily", "report_date": DAY.isoformat(), "source_revision": "source-one",
            "test_run": False, "rerun": 0, "targets": [t.key for t in h.catalog.targets],
        })
        records.save_completed("assessment-settings", AssessmentSettings().to_dict())
        plan = freeze_daily_plan(records, h.catalog.targets, revision="source-one")
        assert all(unit.category is None for unit in plan)
    result = h.daily()
    assert result.category_breakdown is None
    assert "category_breakdown" not in result.to_dict()
    assert "category_policy" not in records.read_completed("run")


def test_missing_category_checkpoint_never_downgrades_a_new_run(harness, monkeypatch):
    h = harness
    result = h.daily()
    original = RecordStore.read_completed

    def missing(records, key, **kwargs):
        return None if key == "result-plan" else original(records, key, **kwargs)

    monkeypatch.setattr(RecordStore, "read_completed", missing)
    before = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)
    with pytest.raises(StateError, match="result_plan_missing"):
        h.daily()
    assert result.category_breakdown is not None
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)) == before


@pytest.mark.parametrize("damage", ["identity", "category", "extra", "policy"])
def test_damaged_frozen_plan_is_not_replaced_from_catalog(harness, monkeypatch, damage):
    h = harness
    h.daily()
    original = RecordStore.read_completed

    def read(records, key, **kwargs):
        value = original(records, key, **kwargs)
        if key == "result-plan":
            value = deepcopy(value)
            if damage == "identity":
                value["units"][1]["unit_id"]["agent"] = "unplanned-agent"
            elif damage == "category":
                value["units"][1]["category"] = "unreviewed"
            elif damage == "extra":
                value["units"][1]["model_output"] = "synthetic-private"
            else:
                value["units"][1].pop("category")
        return value

    monkeypatch.setattr(RecordStore, "read_completed", read)
    before = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)
    with pytest.raises(StateError, match="result_plan_invalid"):
        h.daily()
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)) == before


def test_plan_checkpoint_failure_stops_before_provider_side_effects(harness, monkeypatch):
    original = RecordStore.save_completed

    def fail(records, key, value):
        if key == "result-plan":
            raise CheckpointError()
        return original(records, key, value)

    monkeypatch.setattr(RecordStore, "save_completed", fail)
    with pytest.raises(CheckpointError):
        harness.daily()
    assert not harness.cloud.events and not harness.sol.calls
