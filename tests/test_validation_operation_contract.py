from __future__ import annotations

from agent_insights_quality.live import _trace_assertion_result


def test_validation_operation_sequence_requires_ordered_trace_proof() -> None:
    fixture = {
        "body": {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Synthetic request."}],
                }
            ]
        },
        "trace_assertions": [
            {
                "name": "required_operation_sequence",
                "kind": "operation_sequence",
                "operations": ["invoke_agent", "execute_tool", "chat"],
            }
        ],
    }
    rows = [
        {"operation_name": "invoke_agent", "timestamp": "2026-08-29T00:00:00Z"},
        {"operation_name": "execute_tool", "timestamp": "2026-08-29T00:00:01Z"},
        {"operation_name": "chat", "timestamp": "2026-08-29T00:00:02Z"},
    ]
    assert _trace_assertion_result(rows, fixture)[0].passed is True
    assert _trace_assertion_result(list(reversed(rows)), fixture)[0].passed is True
    out_of_order = [rows[0], rows[2], rows[1]]
    assert _trace_assertion_result(out_of_order, fixture)[0].passed is True
    out_of_order[1]["timestamp"], out_of_order[2]["timestamp"] = (
        out_of_order[2]["timestamp"],
        out_of_order[1]["timestamp"],
    )
    assert _trace_assertion_result(out_of_order, fixture)[0].passed is False


def test_support_validation_rules_include_required_operation_contract() -> None:
    from agent_insights_quality.util import ROOT, read_json

    traffic = read_json(
        ROOT / "agents" / "support-ticket-agent" / "v0" / "traffic.json"
    )
    for attempt in traffic["validation_rules"]["scenarios"][0]["attempts"]:
        assertions = attempt["probe_steps"][0]["expected"]["trace_assertions"]
        operation = next(
            item for item in assertions if item["kind"] == "operation_sequence"
        )
        assert operation["operations"][0] == "invoke_agent"
