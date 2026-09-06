"""Bounded staging collection uses fake clocks and transports, not live telemetry."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from agent_insights_quality.telemetry import Snapshot
import test_runner as fake


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake.fake_storage(monkeypatch)


def evidence_harness(tmp_path, visible, *, hydration=12, query_seconds=0):
    h = fake.Harness(tmp_path, profile="staging", issues=0)
    h.settings = replace(h.settings, hydration_seconds=hydration)
    query = h.cloud.query
    h.query_finished = []

    async def observe(query_text, **kwargs):
        result = await query(query_text, **kwargs)
        ids = {
            "response-" + item[2] for item in h.cloud.invocations
            if visible(item[1].phase, int(item[1].body["input"].split()[-1]), h.clock.elapsed)
        }
        h.clock.value += timedelta(seconds=query_seconds)
        h.clock.elapsed += query_seconds
        h.query_finished.append(h.clock.now())
        return replace(result, records=tuple(
            row for row in result.records if row["customDimensions"]["gen_ai.response.id"] in ids
        ))

    h.cloud.query = observe
    return h


def saved_snapshot(h, result):
    return Snapshot.from_private_dict(
        h.store.run("stage").read_artifact(result["results"][0]["evidence_key"]),
    )


def test_staging_does_not_stop_after_six_roots_and_one_more_poll(tmp_path):
    h = evidence_harness(
        tmp_path, lambda phase, index, elapsed: phase == "setup" or index <= 7 or elapsed >= 4,
    )
    result = h.staging()
    assert result["results"][0]["status"] == "PASS"
    assert result["results"][0]["result"]["passing_attempts"] == 10
    assert h.clock.elapsed == 5
    assert len(saved_snapshot(h, result).attributable_responses) == 20
    assert len(h.cloud.invocations) == 20 and len(h.sol.calls) == 1
    assert not h.cloud.starts


def test_staging_waits_for_setup_roots_used_by_hygiene_not_only_probe_roots(tmp_path):
    h = evidence_harness(
        tmp_path, lambda phase, index, elapsed: phase == "probe" or elapsed >= 3,
    )
    result = h.staging()
    assert result["results"][0]["status"] == "PASS"
    assert h.clock.elapsed == 4
    assert len(saved_snapshot(h, result).attributable_responses) == 20
    assert len(h.cloud.invocations) == 20 and len(h.sol.calls) == 1


def test_staging_rechecks_completeness_on_the_extra_poll(tmp_path):
    h = evidence_harness(
        tmp_path, lambda phase, index, elapsed: (
            elapsed == 0 or elapsed >= 4 or phase == "setup" or index <= 6
        ),
    )
    result = h.staging()
    assert result["results"][0]["result"]["passing_attempts"] == 10
    assert h.clock.elapsed == 5
    assert len(h.cloud.invocations) == 20


@pytest.mark.parametrize("ready,status", [(8, "PASS"), (7, "INCOMPLETE")])
def test_staging_deadline_still_allows_eight_without_requiring_ten_perfect_responses(
    tmp_path, ready, status,
):
    h = evidence_harness(
        tmp_path, lambda phase, index, elapsed: phase == "setup" or index <= ready,
        hydration=5,
    )
    result = h.staging()
    assert h.clock.elapsed == 5
    assert result["results"][0]["status"] == status
    assert result["results"][0]["result"]["passing_attempts"] == ready
    assert len(h.cloud.invocations) == 20 and len(h.sol.calls) == 1
    assert len(h.cloud.deployment_calls) == 1


def test_recovery_recollects_only_evidence_without_repeating_completed_traffic(tmp_path):
    h = evidence_harness(
        tmp_path, lambda phase, index, elapsed: (
            getattr(h, "mature", False) or phase == "setup" or index <= 7
        ),
        hydration=2,
    )
    first = h.staging()
    assert first["results"][0]["status"] == "INCOMPLETE"
    old_snapshot = saved_snapshot(h, first).to_private_dict()
    old_requests = [item[2] for item in h.cloud.invocations]
    old_deployments = dict(h.cloud.deployment_calls)
    work = first["results"][0]["work_key"]
    deadline = h.store.run("stage").read_completed(work + "/evidence/deadline")
    h.mature = True
    second = h.staging()
    assert second["results"][0]["status"] == "PASS"
    assert first["results"][0]["evidence_key"] != second["results"][0]["evidence_key"]
    assert saved_snapshot(h, first).to_private_dict() == old_snapshot
    assert [item[2] for item in h.cloud.invocations] == old_requests
    assert dict(h.cloud.deployment_calls) == old_deployments
    assert h.store.run("stage").read_completed(work + "/evidence/deadline") == deadline
    assert h.clock.elapsed == 2 and len(h.sol.calls) == 2


def test_staging_snapshot_visibility_is_recorded_after_raw_queries_finish(tmp_path):
    h = evidence_harness(tmp_path, lambda *args: True, query_seconds=0.1)
    result = h.staging()
    snapshot = saved_snapshot(h, result)
    assert datetime.fromisoformat(snapshot.observed_at) == h.query_finished[-1]
    assert len(h.cloud.invocations) == 20 and len(h.sol.calls) == 1
