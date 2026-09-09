"""Capture context is evidence metadata, not an automatic semantic judgment."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_insights_quality.telemetry import missing_parent_log_refs
from agent_insights_quality.assessment import assess_daily
from agent_insights_quality.results import UnitId
import test_assessment as fake


def capture_data(*, columnar=False, table="traces"):
    target, attempts, invocations, snapshot = fake.evidence()
    records = list(snapshot.records)
    for index in (3, 6, 8):
        root = f"row-{index}-probe"
        records = [record for record in records if record["ref"] != root]
        rows = [
            (root, {
                "telemetry_table": "requests", "operation_Id": f"operation-{index}",
                "id": f"root-{index}",
            }),
            (f"attached-{index}", {
                "telemetry_table": table, "operation_Id": f"operation-{index}",
                "operation_ParentId": f"root-{index}", "message": "Request started.",
            }),
            (f"orphan-{index}", {
                "telemetry_table": table, "operation_Id": f"operation-{index}",
                "operation_ParentId": "missing-tool", "message": "Function lookup succeeded.",
            }),
        ]
        for ref, raw in rows:
            if columnar:
                raw = {
                    "columns": [{"name": key} for key in raw],
                    "values": list(raw.values()), "table": "synthetic-table",
                }
            records.append({"ref": ref, "raw": raw})
    records += [
        {"ref": "other-operation-parent", "raw": {
            "Type": "AppDependencies", "OperationId": "other-operation", "Id": "missing-tool",
        }},
        {"ref": "unowned-orphan", "raw": {
            "Type": "AppTraces", "OperationId": "unowned-operation",
            "ParentId": "absent-parent", "message": "Unowned completion.",
        }},
    ]
    extra_refs = {
        f"response-{index}-probe": (f"attached-{index}", f"orphan-{index}")
        for index in (3, 6, 8)
    }
    scopes = tuple(replace(
        scope, evidence_refs=(*scope.evidence_refs, *extra_refs.get(scope.response_id, ())),
    ) for scope in snapshot.scopes)
    return target, attempts, invocations, replace(
        snapshot, records=tuple(records), scopes=scopes, gaps=("attributed_log_parent_missing",),
    )


@pytest.mark.parametrize("columnar", [False, True])
@pytest.mark.parametrize("table", ["traces", "AppTraces"])
def test_missing_log_parents_use_operation_and_span_identity_not_global_span_id(columnar, table):
    snapshot = capture_data(columnar=columnar, table=table)[3]
    original = deepcopy(snapshot.to_private_dict())
    assert missing_parent_log_refs(snapshot) == {
        "orphan-3", "orphan-6", "orphan-8", "unowned-orphan",
    }
    assert snapshot.to_private_dict() == original


def test_capture_warnings_are_scoped_and_preserve_raw_evidence_and_citation_authority():
    data = capture_data()
    original = deepcopy(data[3].to_private_dict())
    sol = fake.Sol(lambda payload: fake.output(payload, stage=True))
    result = fake.stage(data, sol)
    payload = sol.calls[0]
    assert payload["snapshot"] == original
    assert result.private_detail["input"] == payload
    for attempt in payload["attempts"]:
        for step in attempt["steps"]:
            if attempt["index"] in (3, 6, 8) and step["phase"] == "probe":
                assert step["trace_capture"] == {
                    "missing_parent_log_refs": [f"orphan-{attempt['index']}"],
                    "span_count_does_not_prove_execution_count": True,
                }
            else:
                assert "trace_capture" not in step
            assert "unowned-orphan" not in step["allowed_citation_refs"]
    assert result.status == "PASS" and result.passing_attempts == 10
    assert data[3].to_private_dict() == original


def test_unresolved_execution_counts_remain_unknown_not_measured_misses():
    def uncertain(payload):
        output = fake.output(payload, stage=True)
        for attempt, judgment in zip(payload["attempts"], output["attempts"], strict=True):
            if any("trace_capture" in step for step in attempt["steps"]):
                judgment.update(
                    sufficient=False, observed=False, contract_violation=False,
                    reason="Synthetic count unresolved; retained spans are only a lower bound.",
                )
        return output

    result = fake.stage(capture_data(), fake.Sol(uncertain))
    assert result.status == "INCOMPLETE"
    assert result.passing_attempts == 7
    assert result.reasons == ("insufficient_evidence", "insufficient_hygiene_evidence")
    assert sum(not judgment["sufficient"] for judgment in result.judgments) == 3


def test_normal_evidence_does_not_acquire_an_invented_capture_gap():
    data = fake.evidence()
    assert not missing_parent_log_refs(data[3])
    result = fake.stage(data)
    assert all(
        "trace_capture" not in step
        for attempt in result.private_detail["input"]["attempts"] for step in attempt["steps"]
    )


def test_capture_context_does_not_raise_daily_readiness_or_exclude_an_empty_baseline():
    target, attempts, invocations, snapshot = capture_data()
    target = replace(target, unit_id=UnitId("weather-agent", "v0"), validation_mode="baseline")
    sol = fake.Sol()
    result = asyncio.run(assess_daily(
        target, attempts, invocations, snapshot, sol,
        before_cards=(), after_cards=(), engine_started_at=fake.ENGINE,
        visible_snapshot=snapshot,
    ))
    payload = sol.calls[0]
    assert payload["measurement_facts"]["attributable_probe_attempts"] == 10
    assert payload["measurement_facts"]["unit_limitations_not_applicable"]
    assert any(
        "trace_capture" in step for attempt in payload["attempts"] for step in attempt["steps"]
    )
    assert not result.unit_result.exclusion_reasons
