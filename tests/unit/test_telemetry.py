import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_insights_quality.contracts import Attempt, Deployment, Invocation, QueryResult, Step, Target
from agent_insights_quality.errors import QualityError
from agent_insights_quality.telemetry import (
    Snapshot,
    collect_snapshot,
    correlate,
    discovery_query,
)
from agent_insights_quality.results import UnitId

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


def test_shared_operation_does_not_make_other_agent_root_or_child_current_evidence():
    rows = [
        span("weather-response", span_id="weather-root"),
        span("healthcare-response", span_id="healthcare-root",
             **{"gen_ai.agent.name": "healthcare-agent"}),
        span("weather-model", span_id="weather-model", parent="weather-root", kind="chat"),
        span("healthcare-model", span_id="healthcare-model", parent="healthcare-root", kind="chat",
             **{"gen_ai.agent.name": "healthcare-agent"}),
    ]
    result = snapshot(rows, ["weather-response"])
    assert result.attributable_responses == {"weather-response"}
    assert result.scopes[0].anchor_refs == ("row-000001",)
    assert set(result.scopes[0].evidence_refs) == {"row-000001", "row-000003"}
    assert len(result.records) == 4
    assert result.records[1]["raw"] == rows[1]
    assert result.records[3]["raw"] == rows[3]


def test_internal_model_output_remains_visible_without_replacing_endpoint_anchor():
    rows = [
        span("endpoint-response", span_id="root",
             **{"gen_ai.output.messages": "Delivered itinerary response."}),
        span("review-response", span_id="review", parent="root", kind="chat",
             **{"travel.review.internal": True, "travel.review.output_delivered": False,
                "gen_ai.output.messages": "Actual internal review text."}),
    ]
    result = snapshot(rows, ["endpoint-response"])
    assert result.scopes[0].anchor_refs == ("row-000001",)
    assert result.scopes[0].evidence_refs == ("row-000001", "row-000002")
    assert result.records[1]["raw"]["customDimensions"]["gen_ai.output.messages"] == "Actual internal review text."


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


def host_root(response="response-a", *, operation="op-1", span_id="host", **extra):
    return {
        **span(response, operation=operation, span_id=span_id, **{
            "azure.ai.agentserver.response_id": response,
            "azure.ai.agentserver.session_id": "session-a",
            "azure.ai.agentserver.conversation_id": "conversation-a",
            "gen_ai.agent.id": "agent-a",
            **extra,
        }),
        "cloud_RoleInstance": "instance-a",
    }


def orphan_log(response="response-a", *, operation="op-1", parent="missing-tool", **extra):
    return {
        "telemetry_table": "traces", "operation_Id": operation,
        "operation_ParentId": parent, "timestamp": START,
        "cloud_RoleInstance": "instance-a",
        "message": "Function get_balance succeeded.",
        "customDimensions": {
            "azure.ai.agentserver.response_id": response,
            "azure.ai.agentserver.session_id": "session-a",
            "azure.ai.agentserver.conversation_id": "conversation-a",
            "gen_ai.agent.name": DEPLOYMENT.agent_name,
            "gen_ai.agent.version": DEPLOYMENT.provider_version,
            "gen_ai.agent.id": "agent-a",
            **extra,
        },
    }


def test_nine_exact_host_lifecycle_logs_survive_missing_execution_parents():
    rows, responses, expected = [], [], {}
    for index in (3, 4, 8):
        response, operation = f"response-{index}", f"op-{index}"
        responses.append(response)
        start = len(rows)
        rows.append(host_root(response, operation=operation))
        for execution in (1, 2):
            tool_span = span(
                "internal", operation=operation, span_id=f"tool-{execution}",
                parent="host", kind="execute_tool",
                **{"gen_ai.tool.name": "get_balance", "gen_ai.tool.call.id": "one-model-call"},
            )
            rows.extend([
                tool_span,
                {**tool_span, "telemetry_table": "genAIContent",
                 "toolCallResult": {"ok": False, "error": {"code": "account_not_found"}}},
                span(
                    "internal", operation=operation, span_id=f"business-{execution}",
                    parent=f"tool-{execution}", kind="execute_tool",
                    **{"aiq.tool.call.result": {"ok": False, "error": {"code": "account_not_found"}}},
                ),
            ])
        for message in (
            "Function name: get_balance", "Function get_balance succeeded.", "Function duration: 0.001s",
        ):
            rows.append({
                **orphan_log(response, operation=operation, parent="tool-3"),
                "message": message,
            })
        expected[response] = {
            f"row-{position:06d}" for position in range(start + 1, len(rows) + 1)
        }
    originals = deepcopy(rows)
    result = snapshot([envelope(row) for row in rows], responses)
    assert rows == originals
    assert result.attributable_responses == set(responses)
    assert result.gaps == ("attributed_log_parent_missing",)
    for scope in result.scopes:
        assert len(scope.anchor_refs) == 1
        assert set(scope.evidence_refs) == expected[scope.response_id]
    assert [record["raw"] for record in result.records] == [envelope(row) for row in rows]
    assert Snapshot.from_private_dict(result.to_private_dict()) == result
    # Both result representations and nested business spans stay raw, not extra invocations.
    assert len(result.scopes) == 3
    assert sum(row.get("id") == "tool-3" for row in rows) == 0
    assert all("account_not_found" not in row["message"] for row in rows if "message" in row)


@pytest.mark.parametrize("field,value", [
    ("azure.ai.agentserver.response_id", "foreign-response"),
    ("response_id", "conflicting-host-response"),
    ("gen_ai.response.id", "response-b"),
    ("azure.ai.agentserver.session_id", "foreign-session"),
    ("azure.ai.agentserver.conversation_id", "foreign-conversation"),
    ("gen_ai.agent.name", "foreign-agent"),
    ("agent.name", "conflicting-agent"),
    ("gen_ai.agent.version", "foreign-version"),
    ("agent.version", "conflicting-version"),
    ("gen_ai.agent.id", "foreign-agent-instance"),
])
@pytest.mark.parametrize("parent", ["host", "missing-tool"])
def test_explicit_log_identity_cannot_override_conflicts(field, value, parent):
    rows = [
        host_root(),
        host_root("response-b", span_id="sibling"),
        orphan_log(parent=parent, **{field: value}),
    ]
    result = snapshot(rows, ["response-a", "response-b"])
    assert "row-000003" not in result.scopes[0].evidence_refs
    assert result.records[2]["raw"] == rows[2]


@pytest.mark.parametrize("parent", ["host", "missing-tool"])
def test_wrong_runtime_instance_log_is_not_attributed(parent):
    row = {**orphan_log(parent=parent), "cloud_RoleInstance": "foreign-instance"}
    result = snapshot([host_root(), row], ["response-a"])
    assert "row-000002" not in result.scopes[0].evidence_refs


def test_nested_framework_instance_is_not_confused_with_remote_hosting_instance():
    rows = [
        {**host_root(), "cloud_RoleInstance": "hosting-gateway"},
        {
            **span("internal", span_id="framework", parent="host"),
            "cloud_RoleInstance": "instance-a",
        },
        orphan_log(),
        {**orphan_log(parent="another-missing-tool"), "cloud_RoleInstance": "foreign-instance"},
    ]
    result = snapshot(rows, ["response-a"])
    assert result.scopes[0].anchor_refs == ("row-000001",)
    assert result.scopes[0].evidence_refs == ("row-000001", "row-000002", "row-000003")


@pytest.mark.parametrize("field", ["cloud_RoleInstance", "azure.ai.agentserver.session_id"])
def test_conflicting_context_on_one_anchor_span_cannot_admit_orphan(field):
    first = host_root()
    second = deepcopy(first)
    if field == "cloud_RoleInstance":
        second[field] = "conflicting-instance"
    else:
        second["customDimensions"][field] = "conflicting-session"
    result = snapshot([first, second, orphan_log()], ["response-a"])
    assert "row-000003" not in result.scopes[0].evidence_refs


@pytest.mark.parametrize("mutation", [
    "same-operation-only", "internal-model-only", "missing-agent", "missing-version",
    "wrong-operation", "missing-parent-id", "content-not-log",
])
def test_orphan_membership_requires_strict_positive_host_evidence(mutation):
    row = orphan_log()
    props = row["customDimensions"]
    if mutation in {"same-operation-only", "internal-model-only"}:
        del props["azure.ai.agentserver.response_id"]
        if mutation == "internal-model-only":
            props["gen_ai.response.id"] = "response-a"
    elif mutation == "missing-agent":
        del props["gen_ai.agent.name"]
    elif mutation == "missing-version":
        del props["gen_ai.agent.version"]
    elif mutation == "wrong-operation":
        row["operation_Id"] = "other-operation"
    elif mutation == "missing-parent-id":
        row.pop("operation_ParentId")
    else:
        row["telemetry_table"] = "genAIContent"
        row["id"] = "missing-tool"
    result = snapshot([host_root(), row], ["response-a"])
    assert result.scopes[0].evidence_refs == ("row-000001",)
    assert not result.gaps


def test_internal_model_response_is_not_confused_with_explicit_host_identity():
    result = snapshot([
        host_root(), orphan_log(**{"gen_ai.response.id": "internal-model-response"}),
    ], ["response-a"])
    assert result.scopes[0].evidence_refs == ("row-000001", "row-000002")


@pytest.mark.parametrize("roots", [
    [],
    [host_root(), host_root(span_id="duplicate-root")],
    [host_root(), host_root(operation="op-2", span_id="other-operation-root")],
    [host_root(**{"gen_ai.agent.version": "different-version"})],
])
def test_orphan_log_cannot_supply_a_missing_or_ambiguous_anchor(roots):
    result = snapshot([*roots, orphan_log()], ["response-a"])
    assert f"row-{len(roots) + 1:06d}" not in result.scopes[0].evidence_refs
    assert "attributed_log_parent_missing" not in result.gaps


def test_exact_host_identity_cannot_override_known_sibling_parent():
    rows = [
        host_root(),
        host_root("response-b", span_id="sibling"),
        span("model-b", span_id="sibling-tool", parent="sibling", kind="execute_tool"),
        orphan_log(parent="sibling-tool"),
    ]
    result = snapshot(rows, ["response-a", "response-b"])
    assert "row-000004" not in result.scopes[0].evidence_refs


def test_orphan_parent_claimed_by_competing_host_logs_is_not_repaired():
    rows = [
        host_root(), host_root("response-b", span_id="sibling"),
        orphan_log(), orphan_log("response-b"),
    ]
    result = snapshot(rows, ["response-a", "response-b"])
    assert all(
        not {"row-000003", "row-000004"} & set(scope.evidence_refs)
        for scope in result.scopes
    )


def test_orphan_parent_with_known_foreign_child_is_not_repaired():
    rows = [
        host_root(),
        {**host_root("response-b", span_id="sibling"), "operation_ParentId": "missing-tool"},
        orphan_log(),
    ]
    result = snapshot(rows, ["response-a", "response-b"])
    assert "row-000003" not in result.scopes[0].evidence_refs


@pytest.mark.parametrize("conflict", [
    {"gen_ai.agent.name": "different-agent"},
    {"gen_ai.agent.version": "different-version"},
    {"azure.ai.agentserver.session_id": "different-session"},
    {"azure.ai.agentserver.response_id": "unplanned-other-host-response"},
])
def test_matching_log_tags_do_not_override_contradictory_ancestry(conflict):
    rows = [
        host_root(),
        span("internal-model", span_id="child", parent="host", kind="chat", **conflict),
        orphan_log(parent="child"),
    ]
    result = snapshot(rows, ["response-a"])
    assert "row-000003" not in result.scopes[0].evidence_refs


def test_exact_host_content_with_sibling_span_stays_outside_scope():
    rows = [
        host_root(), host_root("response-b", span_id="sibling"),
        {
            **orphan_log(),
            "telemetry_table": "genAIContent", "id": "sibling",
            "toolCallResult": {"ok": False, "error": {"code": "account_not_found"}},
        },
    ]
    result = snapshot(rows, ["response-a", "response-b"])
    assert "row-000003" not in result.scopes[0].evidence_refs


def test_collected_orphan_refs_reach_assessment_without_inferred_tool_results():
    from agent_insights_quality.assessment import assess_staging

    rows = [host_root("response-0"), orphan_log("response-0")]

    class Port:
        async def query(self, query, *, start, end):
            selected = rows[:1] if "customDimensions" in query else rows
            return QueryResult(tuple(envelope(row) for row in selected), True)

    captured = []

    class Sol:
        async def complete_json(self, *, instructions, payload, schema):
            captured.append(payload)
            return {"additional_findings": [], "attempts": [
                {
                    "index": index, "sufficient": False, "observed": False,
                    "contract_violation": False,
                    "reason": "Synthetic lifecycle evidence has no third argument/result payload.",
                    "citations": [],
                }
                for index in range(1, 11)
            ]}

    async def run():
        result = await collect_snapshot(
            Port(), DEPLOYMENT, [invocation(0)],
            observed_at=datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
        )
        target = Target(
            UnitId("weather-agent", "issue-001"), "prompt", "model_mediated",
            Path("synthetic-version"), Path("synthetic-baseline"), {"required_executions": 3},
        )
        attempts = tuple(
            Attempt(index, (Step("probe", "probe", {"input": "synthetic"}, {"count": 3}),))
            for index in range(1, 11)
        )
        assessed = await assess_staging(target, attempts, {(1, "probe"): invocation(0)}, result, Sol())
        assert assessed.status == "INCOMPLETE"
        assert assessed.passing_attempts == 0
        return result

    result = asyncio.run(run())
    first = captured[0]["attempts"][0]["steps"][0]
    assert set(first["allowed_citation_refs"]) == {"endpoint-01-01", "row-000001", "row-000002"}
    assert captured[0]["snapshot"]["records"] == list(result.records)
    assert captured[0]["snapshot"]["records"][1]["raw"] == envelope(rows[1])
    assert first["expected"] == {"count": 3}
