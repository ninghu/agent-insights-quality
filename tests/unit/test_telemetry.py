import asyncio
from datetime import UTC, datetime

import pytest

from agent_insights_quality.contracts import Deployment, Invocation, QueryResult
from agent_insights_quality.errors import QualityError
from agent_insights_quality.telemetry import (
    Snapshot,
    collect_snapshot,
    correlate,
    discovery_query,
)

START = "2026-09-04T12:00:00+00:00"
END = "2026-09-04T12:01:00+00:00"
DEPLOYMENT = Deployment("weather-agent/v0", "weather-agent", "7", "prompt", "source")


def span(response, *, operation="op-1", span_id=None, parent="", kind="invoke_agent", **extra):
    return {
        "telemetry_table": "requests" if kind == "invoke_agent" else "dependencies",
        "operation_Id": operation,
        "id": span_id or response,
        "operation_ParentId": parent,
        "timestamp": START,
        "customDimensions": {
            "gen_ai.response.id": response,
            "gen_ai.operation.name": kind,
            "gen_ai.agent.name": "weather-agent",
            "gen_ai.agent.version": "7",
            **extra,
        },
    }


def snapshot(rows, responses):
    return correlate(
        rows, responses, DEPLOYMENT,
        observed_at=END, window_start=START, window_end=END,
    )


def envelope(row):
    return {
        "table": "PrimaryResult",
        "columns": [{"name": name} for name in row],
        "values": list(row.values()),
    }


def test_six_responses_can_share_one_operation():
    responses = [f"response-{index}" for index in range(6)]
    result = snapshot([span(value) for value in responses], responses)
    assert result.attributable_responses == set(responses)
    assert {scope.operation_ids for scope in result.scopes} == {("op-1",)}


def test_nested_host_anchors_and_raw_payloads_are_preserved():
    rows = [
        span("response-a", span_id="host"),
        span("response-a", span_id="framework", parent="host"),
        span("model-response", span_id="model", parent="framework", kind="chat",
             **{"gen_ai.input.messages": [{"content": "actual synthetic input"}],
                "future.attribute": {"not": "discarded"}}),
        span("response-b", span_id="foreign"),
    ]
    result = snapshot([envelope(row) for row in rows], ["response-a", "response-b"])
    first = result.scopes[0]
    assert first.attributable
    assert first.anchor_refs == ("row-000001",)
    assert set(first.evidence_refs) == {"row-000001", "row-000002", "row-000003"}
    assert result.records[2]["raw"] == envelope(rows[2])
    assert len(result.records) == 4
    assert Snapshot.from_private_dict(result.to_private_dict()) == result


def test_unrelated_exact_roots_are_ambiguous():
    result = snapshot(
        [span("response-a", span_id="one"), span("response-a", span_id="two")],
        ["response-a"],
    )
    assert not result.attributable_responses
    assert "invocation_anchor_ambiguous" in result.scopes[0].reasons


def test_only_model_response_identity_is_not_endpoint_proof():
    result = snapshot(
        [span("other-response", span_id="host"),
         span("response-a", span_id="model", parent="host", kind="chat")],
        ["response-a"],
    )
    assert not result.attributable_responses


@pytest.mark.parametrize("field,value", [
    ("gen_ai.agent.name", "different-agent"),
    ("gen_ai.agent.version", "different-version"),
])
def test_observed_wrong_identity_does_not_count(field, value):
    result = snapshot([span("response-a", **{field: value})], ["response-a"])
    assert not result.attributable_responses


def test_missing_redundant_identity_fields_does_not_hide_exact_response():
    row = span("response-a")
    del row["customDimensions"]["gen_ai.agent.version"]
    result = snapshot([row], ["response-a"])
    assert result.attributable_responses == {"response-a"}


def test_transport_reference_connects_to_real_invocation():
    rows = [
        span("response-a", span_id="http", kind="http"),
        span("internal-id", span_id="agent", parent="http"),
        span("model-id", span_id="model", parent="agent", kind="chat"),
    ]
    result = snapshot(rows, ["response-a"])
    assert result.attributable_responses == {"response-a"}
    assert len(result.scopes[0].evidence_refs) == 3


def test_logs_and_content_are_context_not_extra_span_nodes():
    root = span("response-a", span_id="host")
    log = {
        "telemetry_table": "traces", "operation_Id": "op-1",
        "operation_ParentId": "host", "message": "synthetic detail",
    }
    content = {
        "telemetry_table": "genAIContent", "operation_Id": "op-1",
        "id": "host", "outputMessages": [{"content": "actual output"}],
    }
    result = snapshot([root, log, content], ["response-a"])
    assert result.scopes[0].anchor_refs == ("row-000001",)
    assert len(result.scopes[0].evidence_refs) == 3


def test_compatible_duplicates_remain_raw_without_inflating_invocations():
    root = span("response-a")
    richer = {**root, "customMeasurements": {"duration": 1}}
    result = snapshot([root, dict(root), richer], ["response-a"])
    assert result.attributable_responses == {"response-a"}
    assert len(result.records) == 3


def test_conflicting_same_span_and_cycles_are_not_silently_repaired():
    conflict = snapshot(
        [span("response-a", parent="one"), span("response-a", parent="two")],
        ["response-a"],
    )
    assert not conflict.attributable_responses
    cycle = snapshot(
        [span("response-a", span_id="host", parent="model"),
         span("internal", span_id="model", parent="host", kind="chat")],
        ["response-a"],
    )
    assert not cycle.attributable_responses
    assert "span_parent_cycle" in cycle.scopes[0].reasons


def test_discovery_uses_each_identity_field_without_a_coalesce():
    query = discovery_query(["response'\\value"], START, END)
    assert "coalesce" not in query
    assert "azure.ai.agentserver.response_id" in query
    assert "response\\'\\\\value" in query


def test_bad_envelope_is_not_a_successful_empty_record():
    with pytest.raises(QualityError, match="telemetry_columns_invalid"):
        snapshot([{"columns": [{"name": "id"}], "values": []}], ["response-a"])


def test_snapshot_cannot_restore_fictional_evidence_references():
    value = snapshot([span("response-a")], ["response-a"]).to_private_dict()
    value["scopes"][0]["evidence_refs"] = ["missing-row"]
    with pytest.raises(QualityError, match="evidence_snapshot_format_invalid"):
        Snapshot.from_private_dict(value)


def invocation(index):
    return Invocation(
        f"request-{index}", f"response-{index}", None,
        START, END, "completed", {"id": f"response-{index}"},
    )


def test_collection_splits_large_operation_batches_without_losing_records():
    rows = [
        envelope(span(f"response-{index}", operation=f"op-{index}"))
        for index in range(2)
    ]

    class Port:
        def __init__(self):
            self.calls = []

        async def query(self, query, *, start, end):
            self.calls.append(query)
            if "customDimensions" in query:
                return QueryResult(tuple(rows), True)
            selected = [row for index, row in enumerate(rows) if f"'op-{index}'" in query]
            if len(selected) > 1:
                return QueryResult((), False, "result_too_large")
            return QueryResult(tuple(selected), True)

    port = Port()
    result = asyncio.run(collect_snapshot(
        port, DEPLOYMENT, [invocation(0), invocation(1)],
        observed_at=datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
    ))
    assert len(port.calls) == 4
    assert result.query_complete
    assert result.attributable_responses == {"response-0", "response-1"}
    assert len(result.records) == 2


def test_partial_result_is_visible_not_claimed_complete():
    class Port:
        async def query(self, query, *, start, end):
            return QueryResult((span("response-0"),), False, "partial_query")

    result = asyncio.run(collect_snapshot(Port(), DEPLOYMENT, [invocation(0)]))
    assert not result.query_complete
    assert "partial_query" in result.gaps


def test_duplicate_endpoint_ids_fail_before_any_query():
    class Port:
        async def query(self, *args, **kwargs):
            raise AssertionError("No query should be issued")

    with pytest.raises(QualityError, match="duplicate_endpoint_response"):
        asyncio.run(collect_snapshot(Port(), DEPLOYMENT, [invocation(0), invocation(0)]))
