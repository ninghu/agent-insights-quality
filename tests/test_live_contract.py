from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
import types
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.live import (
    LiveRuntime,
    RemoteOperationError,
    TelemetryCorrelationError,
    TelemetryQueryError,
    _canonical_output_messages_expectation_passes,
    _complete_operation_ids,
    _correlated_request_rows,
    _normalize_fixture,
    _prompt_agent_route_propagation_pending,
    _semantic_assertion_result,
    _trace_assertion_result,
    _trace_behavior_summary,
    _trace_contract_ready,
    _usable_response,
)
from agent_insights_quality.models import InsightRunCheckpoint, InvocationEvidence
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    InsightWindowExpiredError,
    TraceAssertionActivationError,
    read_json,
)


def _runtime() -> LiveRuntime:
    return LiveRuntime(
        RuntimeProfile(
            name="daily",
            project_name="agent-insights-quality",
            project_endpoint="https://example.invalid",
            insights_endpoint="https://example.invalid",
            application_insights_resource_id="/subscriptions/hidden/resourceGroups/hidden/providers/Microsoft.Insights/components/hidden",
            registry_path=Path("registry.json"),
        ),
        token_provider=lambda _: "synthetic-token",
    )


def _install_logs_query_status(monkeypatch):
    statuses = type(
        "LogsQueryStatus",
        (),
        {
            "SUCCESS": "success",
            "PARTIAL": "partial",
            "FAILURE": "failure",
        },
    )
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = statuses
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    return statuses


def _disable_traffic_ledger(runtime: LiveRuntime) -> None:
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {"mark_started": staticmethod(lambda *_args, **_kwargs: None)},
    )()


def _text_response(text: str) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def _prompt_issue_fixture(
    agent_name: str,
    issue_id: str,
    request_index: int = 0,
) -> dict:
    traffic = read_json(
        ROOT
        / "agents"
        / agent_name
        / "issues"
        / issue_id
        / "traffic.json"
    )
    return _normalize_fixture(traffic["requests"][request_index])


def _all_semantic_assertions_pass(fixture: dict, text: str) -> bool:
    count, passed, _ = _semantic_assertion_result(_text_response(text), fixture)
    return count > 0 and passed == count


def test_fixture_preserves_expected_failure_and_conversation() -> None:
    value = _normalize_fixture(
        {
            "id": "issue-032-request-1",
            "request": {
                "body": {
                    "input": "synthetic request",
                    "conversation": {"id": "shared-conversation"},
                }
            },
            "expected": {"http_status": 422},
        }
    )
    assert value["expected_status"] == 422
    assert value["conversation_key"] == "shared-conversation"


def test_usable_response_requires_independent_output_or_expected_error() -> None:
    assert _usable_response({"output": []}, 422) is True
    assert _usable_response({"output": []}, 200) is False
    assert (
        _usable_response(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Synthetic answer"}],
                    }
                ]
            },
            200,
        )
        is True
    )


def test_semantic_assertions_record_only_counts() -> None:
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Forecast high 18 and low 11.",
                    }
                ],
            }
        ]
    }
    count, passed, results = _semantic_assertion_result(
        response,
        {
            "semantic_assertions": {
                "response_format": "non_json",
                "required_terms_any": ["high", "low"],
                "forbidden_terms": ["temporary_unavailable"],
            }
        },
    )
    assert count == 3
    assert passed == 3
    assert [item.assertion for item in results] == [
        "response_format",
        "required_terms_any",
        "forbidden_terms",
    ]


def test_semantic_assertions_support_structured_and_bounded_evidence() -> None:
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"condition":"clear","temperature":21,"unit":"C"}',
                    }
                ],
            }
        ]
    }
    count, passed, results = _semantic_assertion_result(
        response,
        {
            "semantic_assertions": {
                "response_format": "json",
                "json_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["condition", "temperature", "unit"],
                    "properties": {
                        "condition": {"const": "clear"},
                        "temperature": {"type": "integer"},
                        "unit": {"const": "C"},
                    },
                },
                "exact_json_fields": {"condition": "clear", "unit": "C"},
                "exact_json": {
                    "condition": "clear",
                    "temperature": 21,
                    "unit": "C",
                },
                "required_claims": ['"temperature":21'],
                "forbidden_claims": ["unavailable"],
                "min_words": 1,
                "max_words": 3,
                "max_characters": 80,
            }
        },
    )
    assert count == passed == 9
    assert all(item.passed for item in results)


@pytest.mark.parametrize(
    ("agent_name", "issue_id", "request_index", "defect_text", "healthy_text"),
    [
        (
            "weather-agent",
            "issue-002",
            0,
            "Clear conditions, 21 celsius.",
            '{"condition":"clear","temperature":21,"unit":"celsius"}',
        ),
        (
            "weather-agent",
            "issue-003",
            0,
            '{"shape":"forecast","high":27,"low":13,"unit":"celsius","note":"forecast evidence"}',
            '{"condition":"clear","temperature":19,"unit":"celsius"}',
        ),
        (
            "weather-agent",
            "issue-004",
            1,
            '{"condition":"clear","temperature":68,"unit":"fahrenheit","source":"request"}',
            '{"condition":"clear","temperature":68,"unit":"celsius"}',
        ),
        (
            "weather-agent",
            "issue-005",
            0,
            "Should I use the complete evidence you supplied?",
            "Conditions are clear at 20 celsius. Would you like anything else?",
        ),
        (
            "weather-agent",
            "issue-006",
            0,
            "Clear at 22 celsius. " * 10,
            "Clear at 22 celsius.",
        ),
        (
            "healthcare-agent",
            "issue-008",
            0,
            '{"action":"create_appointment","provider":"Dr. Rivera","slot":"slot-demo-101","message":"Please confirm"}',
            '{"action":"create_appointment","provider":"Dr. Rivera","slot":"slot-demo-101","account_scope":"demo-account-a","approval":"pending","message":"Please confirm"}',
        ),
        (
            "healthcare-agent",
            "issue-010",
            0,
            '{"availability":"available","provider":"Dr. Rivera","date":"2026-09-21","slot":"slot-demo-101","account_scope":"demo-account-a","evidence":"missing"}',
            '{"availability":"unknown","provider":"Dr. Rivera","date":"2026-09-21","slot":"slot-demo-101","account_scope":"demo-account-a","evidence":"missing"}',
        ),
        (
            "healthcare-agent",
            "issue-011",
            0,
            '{"action":"transition_appointment_state","provider":"Dr. Rivera","slot":"slot-demo-101","account_scope":"demo-account-a","confirmation":false,"state":"confirmed"}',
            '{"action":"transition_appointment_state","provider":"Dr. Rivera","slot":"slot-demo-101","account_scope":"demo-account-a","confirmation":false,"state":"pending"}',
        ),
        (
            "healthcare-agent",
            "issue-012",
            0,
            '{"account_scope":"demo-account-b","record_id":"record-b-1","provider":"Dr. Chen","slot":"slot-demo-b1"}',
            '{"account_scope":"demo-account-a","appointments":[],"message":"I cannot disclose foreign records"}',
        ),
    ],
)
def test_r03_prompt_oracles_accept_defects_and_reject_healthy_contradictions(
    agent_name: str,
    issue_id: str,
    request_index: int,
    defect_text: str,
    healthy_text: str,
) -> None:
    fixture = _prompt_issue_fixture(agent_name, issue_id, request_index)

    assert _all_semantic_assertions_pass(fixture, defect_text)
    assert not _all_semantic_assertions_pass(fixture, healthy_text)


def test_exact_json_assertions_distinguish_booleans_from_numbers() -> None:
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"completed":0,"nested":[1]}',
                    }
                ],
            }
        ]
    }
    count, passed, _ = _semantic_assertion_result(
        response,
        {
            "semantic_assertions": {
                "exact_json": {
                    "completed": False,
                    "nested": [True],
                }
            }
        },
    )
    assert count == 1
    assert passed == 0


def test_weather_v0_assertions_reject_additional_fabricated_claims() -> None:
    requests = read_json(
        ROOT / "agents" / "weather-agent" / "v0" / "traffic.json"
    )["requests"]
    for request in requests:
        assertions = request["expected"]["semantic_assertions"]
        canonical = assertions.get("exact_text")
        if canonical is None:
            canonical_json = {
                **assertions["casefold_json_fields"],
                **assertions["exact_json_fields"],
            }
            canonical = json.dumps(
                canonical_json,
                separators=(",", ":"),
            )
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": canonical}],
                }
            ]
        }
        count, passed, _ = _semantic_assertion_result(
            response,
            request["expected"],
        )
        assert count == passed

    adversarial_outputs = (
        "Austin: sunny, 86 Fahrenheit. Humidity is 45 percent.",
        "Denver: snow, 25 Fahrenheit. Visibility is 10 miles.",
        "Oslo tomorrow: cloudy, high 12 Celsius, low 7 Celsius. Rain is likely.",
        '{"condition":"clear","temperature":21,"unit":"celsius","humidity":45}',
        (
            "Reykjavik day one: windy, high 9 Celsius, low 5 Celsius. "
            "Day two: unavailable. Day two will be sunny."
        ),
    )
    for request, text in zip(requests, adversarial_outputs, strict=True):
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        }
        count, passed, _ = _semantic_assertion_result(
            response,
            request["expected"],
        )
        assert count > 0
        assert passed < count


@pytest.mark.parametrize(
    ("text", "expected_passed"),
    [
        ('{"condition":"clear","temperature":21,"unit":"celsius"}', 4),
        ('{"unit":"CELSIUS","temperature":21,"condition":"CLEAR"}', 4),
        ("Weather summary: clear, 21 celsius.", 0),
        (
            '```json\n{"condition":"clear","temperature":21,"unit":"celsius"}\n```',
            0,
        ),
        ('{"condition":"rain","temperature":21,"unit":"celsius"}', 3),
        ('{"condition":"not clear","temperature":21,"unit":"celsius"}', 3),
        ('{"condition":"clear skies","temperature":21,"unit":"celsius"}', 3),
        ('{"condition":["clear"],"temperature":21,"unit":"celsius"}', 2),
        ('{"condition":"clear","temperature":"21","unit":"celsius"}', 2),
        (
            '{"condition":"clear","temperature":21,"unit":"celsius",'
            '"humidity":45}',
            3,
        ),
    ],
)
def test_weather_structured_output_assertions_reject_drift(
    text: str,
    expected_passed: int,
) -> None:
    expected = read_json(
        ROOT / "agents" / "weather-agent" / "v0" / "traffic.json"
    )["requests"][3]["expected"]
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }
    count, passed, _ = _semantic_assertion_result(response, expected)
    assert count == 4
    assert passed == expected_passed


@pytest.mark.parametrize(
    ("text", "expected_passed"),
    [
        (
            '{"action":"create_appointment","provider":"Dr. Ali",'
            '"slot":"slot-demo-505","account_scope":"demo-account-b",'
            '"approval":"pending","message":"Please confirm",'
            '"warning":"2026-09-15 unavailable"}',
            3,
        ),
        (
            '{"warning":"2026-09-15 unavailable","message":"Please confirm",'
            '"approval":"pending","account_scope":"demo-account-b",'
            '"slot":"slot-demo-505","provider":"Dr. Ali",'
            '"action":"create_appointment"}',
            3,
        ),
        ("Please confirm the appointment.", 0),
        (
            '```json\n{"action":"create_appointment","provider":"Dr. Ali"}\n```',
            0,
        ),
        (
            '{"action":"create_appointment","provider":"Dr. Ali",'
            '"slot":"slot-demo-505","account_scope":"demo-account-b",'
            '"approval":"confirmed","message":"Please confirm",'
            '"warning":"2026-09-15 unavailable"}',
            2,
        ),
        (
            '{"action":"create_appointment","provider":"Dr. Ali",'
            '"slot":"slot-demo-999","account_scope":"demo-account-b",'
            '"approval":"pending","message":"Please confirm",'
            '"warning":"2026-09-15 unavailable"}',
            2,
        ),
        (
            '{"action":"create_appointment","provider":"Dr. Ali",'
            '"slot":505,"account_scope":"demo-account-b",'
            '"approval":"pending","message":"Please confirm",'
            '"warning":"2026-09-15 unavailable"}',
            1,
        ),
        (
            '{"action":"create_appointment","provider":"Dr. Ali",'
            '"slot":"slot-demo-505","account_scope":"demo-account-b",'
            '"approval":"pending","message":"Please confirm",'
            '"warning":"2026-09-15 unavailable","extra":"unsafe"}',
            1,
        ),
    ],
)
def test_healthcare_structured_output_assertions_reject_drift(
    text: str,
    expected_passed: int,
) -> None:
    expected = read_json(
        ROOT / "agents" / "healthcare-agent" / "v0" / "traffic.json"
    )["requests"][4]["expected"]
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }
    count, passed, _ = _semantic_assertion_result(response, expected)
    assert count == 3
    assert passed == expected_passed


def test_healthcare_v0_exact_text_rejects_contradictory_answers() -> None:
    requests = read_json(
        ROOT / "agents" / "healthcare-agent" / "v0" / "traffic.json"
    )["requests"][:4]
    for request in requests:
        expected = request["expected"]
        exact_text = expected["semantic_assertions"]["exact_text"]
        correct_response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": exact_text}],
                }
            ]
        }
        count, passed, _ = _semantic_assertion_result(correct_response, expected)
        assert count == passed

        for adversarial_text in (
            f"{exact_text} That slot is closed.",
            f"{exact_text} That slot is unavailable.",
            f"{exact_text} That availability is not correct.",
            f"{exact_text} Ignore that stale provider; use Dr. Patel.",
            f"{exact_text} Ignore that wrong scope; use demo-account-z.",
        ):
            adversarial_response = {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": adversarial_text}
                        ],
                    }
                ]
            }
            count, passed, _ = _semantic_assertion_result(
                adversarial_response,
                expected,
            )
            assert count > 0
            assert passed < count


@pytest.mark.parametrize(
    "text",
    [
        "Weather summary: clear, 21 celsius is not correct.",
        "Weather summary: clear, 21 celsius. That complete summary is not correct.",
    ],
)
def test_issue_002_structural_oracle_rejects_postposed_contradiction(
    text: str,
) -> None:
    expected = read_json(
        ROOT
        / "agents"
        / "weather-agent"
        / "issues"
        / "issue-002"
        / "traffic.json"
    )["requests"][0]["expected"]
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }
    count, passed, _ = _semantic_assertion_result(
        response,
        expected,
    )
    assert count == 3
    assert passed == 2


def test_issue_006_structural_oracle_rejects_long_postposed_contradiction() -> None:
    expected = read_json(
        ROOT
        / "agents"
        / "weather-agent"
        / "issues"
        / "issue-006"
        / "traffic.json"
    )["requests"][0]["expected"]
    contradicted = (
        ("Clear at 22 celsius. " * 10)
        + "Despite all of that repeated detail, this summary is not correct."
    )
    assert len(re.findall(r"\S+", contradicted)) > 40
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": contradicted}],
            }
        ]
    }
    count, passed, _ = _semantic_assertion_result(response, expected)
    assert count == 3
    assert passed == 2


@pytest.mark.parametrize(
    ("issue_id", "wrong_value"),
    [
        ("issue-002", "20 celsius"),
        ("issue-006", "23 celsius"),
    ],
)
def test_structural_weather_gates_reject_wrong_numeric_claims(
    issue_id: str,
    wrong_value: str,
) -> None:
    expected = read_json(
        ROOT
        / "agents"
        / "weather-agent"
        / "issues"
        / issue_id
        / "traffic.json"
    )["requests"][0]["expected"]
    wrong_text = (
        f"Clear conditions at {wrong_value}."
        if issue_id == "issue-002"
        else (f"Clear at {wrong_value}. " * 10)
    )
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": wrong_text}],
            }
        ]
    }
    count, passed, _ = _semantic_assertion_result(response, expected)
    assert passed < count


def test_support_partial_exact_text_rejects_paraphrase() -> None:
    expected = read_json(
        ROOT / "agents" / "support-ticket-agent" / "v0" / "traffic.json"
    )["requests"][4]["expected"]
    exact_text = expected["semantic_assertions"]["exact_text"]
    for text, expected_passed in (
        (exact_text, 1),
        (
            "Ticket ticket-demo-1 is open at revision 3; its optional "
            "history is unavailable.",
            0,
        ),
    ):
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        }
        count, passed, _ = _semantic_assertion_result(response, expected)
        assert count == 1
        assert passed == expected_passed


def test_trace_behavior_summary_sanitizes_prompt_tool_sequence() -> None:
    messages = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "lookup_slots",
                        "arguments": {"private": "not retained"},
                    }
                ],
            },
            {
                "role": "tool",
                "parts": [
                    {
                        "type": "tool_call_response",
                        "response": json.dumps(
                            {
                                "error": {
                                    "code": "temporary_unavailable",
                                    "retryable": True,
                                }
                            }
                        ),
                    }
                ],
            },
        ]
    )
    summary = _trace_behavior_summary(
        [
            {
                "operation_id": "a" * 32,
                "operation_name": "invoke_agent",
                "tool_name": "",
                "tool_call_id": "",
                "error_type": "",
                "tool_ok": "",
                "tool_result": "",
                "messages": ["", messages],
                "timestamp": "2026-08-26T10:00:00Z",
            }
        ]
    )
    assert summary == {
        "operation_count": 1,
        "tool_call_counts": {"lookup_slots": 1},
        "tool_response_count": 1,
        "successful_tool_response_count": 0,
        "error_codes": {"temporary_unavailable": 1},
        "assistant_response_count": 0,
        "explicit_terminal_success_count": 0,
        "explicit_terminal_output_count": 0,
        "terminal_success_count": 0,
        "terminal_output_count": 0,
        "terminal_response_count": 0,
        "handled_error_count": 0,
        "unhandled_error_count": 0,
    }
    assert "private" not in json.dumps(summary)


def _tool_trace_row(
    tool_name: str,
    *,
    arguments: dict | None = None,
    result: dict | None = None,
    ok: bool | None = None,
    timestamp: str = "2026-08-28T10:00:00+00:00",
    duration: str | float = 10.0,
) -> dict:
    return {
        "operation_id": "a" * 32,
        "operation_name": "execute_tool",
        "tool_name": tool_name,
        "tool_call_id": "",
        "error_type": "",
        "tool_ok": "" if ok is None else str(ok).lower(),
        "tool_result": "" if result is None else json.dumps(result),
        "tool_arguments": "" if arguments is None else json.dumps(arguments),
        "messages": ["", ""],
        "timestamp": timestamp,
        "duration": duration,
        "span_name": f"tool.{tool_name}",
        "terminal_success": "",
        "terminal_output": "",
        "handled_error": "",
        "matched_reference": "",
    }


def _terminal_trace_row(
    text: str,
    *,
    input_messages: list[dict] | None = None,
) -> dict:
    row = _tool_trace_row("", timestamp="2026-08-28T10:00:01+00:00")
    row.update(
        {
            "operation_name": "chat",
            "messages": [
                json.dumps(input_messages or []),
                json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [{"type": "text", "content": text}],
                        }
                    ]
                ),
            ],
            "span_name": "model.respond",
        }
    )
    return row


@pytest.mark.parametrize(
    ("issue_id", "rows", "bad_rows"),
    [
        (
            "issue-013",
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-a"},
                    result={
                        "ok": True,
                        "account_id": "acct-demo-a",
                        "balance": 1250.5,
                    },
                    ok=True,
                ),
                _terminal_trace_row(
                    "The authoritative balance for acct-demo-a is USD 1750.50."
                ),
            ],
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-a"},
                    result={"ok": True, "balance": 1250.5},
                    ok=True,
                ),
                _terminal_trace_row("The authoritative balance is USD 1250.5."),
            ],
        ),
        (
            "issue-014",
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={},
                    result={
                        "ok": False,
                        "error": {"code": "account_id_required"},
                    },
                    ok=False,
                ),
                _terminal_trace_row("account_id was omitted."),
            ],
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-a"},
                    result={"ok": True, "balance": 1250.5},
                    ok=True,
                ),
                _terminal_trace_row("The balance is available."),
            ],
        ),
        (
            "issue-015",
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-b"},
                    result={"ok": True, "account_id": "acct-demo-b"},
                    ok=True,
                ),
                _terminal_trace_row("acct-demo-b has USD 875.00."),
            ],
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-a"},
                    result={"ok": True, "account_id": "acct-demo-a"},
                    ok=True,
                ),
                _terminal_trace_row("acct-demo-a has USD 1250.50."),
            ],
        ),
        (
            "issue-016",
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-missing"},
                    result={
                        "ok": False,
                        "error": {"code": "account_not_found"},
                    },
                    ok=False,
                ),
                _terminal_trace_row(
                    "The successful balance is account_not_found."
                ),
            ],
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-missing"},
                    result={
                        "ok": False,
                        "error": {"code": "account_not_found"},
                    },
                    ok=False,
                ),
                _terminal_trace_row("The lookup failed."),
            ],
        ),
        (
            "issue-017",
            [
                _tool_trace_row(
                    "get_budget_summary",
                    result={"ok": True},
                    ok=True,
                ),
                _tool_trace_row(
                    "get_budget_summary",
                    result={
                        "ok": False,
                        "error": {"code": "account_not_found"},
                    },
                    ok=False,
                    timestamp="2026-08-28T10:00:00.100+00:00",
                ),
                _terminal_trace_row("The complete budget summary is ready."),
            ],
            [
                _tool_trace_row(
                    "get_budget_summary",
                    result={"ok": True},
                    ok=True,
                ),
                _tool_trace_row(
                    "get_budget_summary",
                    result={
                        "ok": False,
                        "error": {"code": "account_not_found"},
                    },
                    ok=False,
                ),
                _terminal_trace_row("The partial budget summary is ready."),
            ],
        ),
        (
            "issue-018",
            [
                _tool_trace_row(
                    "get_balance_with_transient",
                    result={
                        "ok": False,
                        "error": {"code": "temporary_unavailable"},
                    },
                    ok=False,
                )
            ],
            [
                _tool_trace_row(
                    "get_balance_with_transient",
                    result={
                        "ok": False,
                        "error": {"code": "temporary_unavailable"},
                    },
                    ok=False,
                ),
                _tool_trace_row(
                    "get_balance_with_transient",
                    result={"ok": True},
                    ok=True,
                ),
            ],
        ),
        (
            "issue-019",
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-missing"},
                    result={"ok": False, "error": {"code": "account_not_found"}},
                    ok=False,
                    timestamp=f"2026-08-28T10:00:00.{index}00+00:00",
                )
                for index in range(3)
            ],
            [
                _tool_trace_row(
                    "get_balance",
                    arguments={"account_id": "acct-demo-missing"},
                    result={"ok": False, "error": {"code": "account_not_found"}},
                    ok=False,
                )
            ],
        ),
        (
            "issue-020",
            [
                _terminal_trace_row(
                    "The finance summary is ready.",
                    input_messages=[
                        {"role": "user", "parts": [{"type": "text", "content": "x"}]}
                    ]
                    * 4,
                )
            ],
            [
                _terminal_trace_row(
                    "The finance summary is ready.",
                    input_messages=[
                        {"role": "user", "parts": [{"type": "text", "content": "x"}]}
                    ],
                )
            ],
        ),
    ],
)
def test_finance_trace_assertions_cover_r01_failures(
    issue_id: str,
    rows: list[dict],
    bad_rows: list[dict],
) -> None:
    request = read_json(
        ROOT
        / "agents"
        / "finance-agent"
        / "issues"
        / issue_id
        / "traffic.json"
    )["requests"][0]
    fixture = _normalize_fixture(request)
    passing = _trace_assertion_result(rows, fixture)
    failing = _trace_assertion_result(bad_rows, fixture)
    assert passing and all(item.passed for item in passing)
    assert any(not item.passed for item in failing)
    serialized = json.dumps(
        [item.__dict__ for item in passing],
        sort_keys=True,
    )
    assert "acct-demo" not in serialized
    assert "account_not_found" not in serialized


def _anchor_row(
    operation_id: str,
    span_id: str,
    reference: str,
    *,
    parent: str = "",
    operation_name: str = "invoke_agent",
    agent_name: str = "healthcare-agent-issue-010",
    agent_version: str = "7",
) -> dict:
    return {
        "operation_id": operation_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "operation_name": operation_name,
        "matched_reference": reference,
        "agent_name": agent_name,
        "agent_version": agent_version,
    }


def test_issue_010_011_shared_operation_uses_independent_response_anchors() -> None:
    operation_a = "a" * 32
    operation_b = "b" * 32
    rows = [
        _anchor_row(operation_a, "root-a", "response-1"),
        _anchor_row(operation_b, "root-b", "response-2"),
    ]
    correlated = _correlated_request_rows(
        rows,
        ("response-1", "response-2"),
        (operation_a, operation_b),
        agent_name="healthcare-agent-issue-010",
        foundry_version="7",
    )
    assert correlated is not None
    subtrees, anchors = correlated
    assert [items[0]["operation_id"] for items in subtrees] == [
        operation_a,
        operation_b,
    ]
    assert anchors == ("root-a", "root-b")
    shared = "c" * 32
    shared_rows = [
        _anchor_row(shared, "root-1", "response-1"),
        _anchor_row(
            shared,
            "child-1",
            "",
            parent="root-1",
            operation_name="chat",
        ),
        _anchor_row(shared, "root-2", "response-2"),
        _anchor_row(
            shared,
            "child-2",
            "",
            parent="root-2",
            operation_name="tool",
        ),
    ]
    correlated_shared = _correlated_request_rows(
        shared_rows,
        ("response-1", "response-2"),
        (shared, shared),
        agent_name="healthcare-agent-issue-010",
        foundry_version="7",
    )
    assert correlated_shared is not None
    shared_subtrees, shared_anchors = correlated_shared
    assert [
        {item["span_id"] for item in request_rows}
        for request_rows in shared_subtrees
    ] == [{"root-1", "child-1"}, {"root-2", "child-2"}]
    assert shared_anchors == ("root-1", "root-2")


def test_response_anchor_ignores_idless_trace_rows() -> None:
    operation_id = "c" * 32
    rows = [
        _anchor_row(operation_id, "root", "response-1"),
        _anchor_row(
            operation_id,
            "child",
            "",
            parent="root",
            operation_name="chat",
        ),
        {
            "operation_id": operation_id,
            "span_id": "",
            "parent_span_id": "",
            "operation_name": "log",
            "matched_reference": "",
            "messages": ["", "must-not-contribute"],
        },
    ]
    correlation = _correlated_request_rows(
        rows,
        ("response-1",),
        (operation_id,),
        agent_name="healthcare-agent-issue-010",
        foundry_version="7",
    )
    assert correlation is not None
    subtrees, anchors = correlation
    assert anchors == ("root",)
    assert {row["span_id"] for row in subtrees[0]} == {"root", "child"}


@pytest.mark.parametrize("telemetry_type", ["requests", "dependencies"])
def test_response_anchor_allows_its_external_parent_boundary(
    telemetry_type,
) -> None:
    operation_id = "c" * 32
    anchor = _anchor_row(
        operation_id,
        "root",
        "response-1",
        parent="upstream-span-outside-query",
    )
    anchor["telemetry_type"] = telemetry_type
    rows = [
        anchor,
        _anchor_row(
            operation_id,
            "child",
            "",
            parent="root",
            operation_name="chat",
        ),
    ]
    correlation = _correlated_request_rows(
        rows,
        ("response-1",),
        (operation_id,),
        agent_name="healthcare-agent-issue-010",
        foundry_version="7",
    )
    assert correlation is not None
    subtrees, anchors = correlation
    assert anchors == ("root",)
    assert {row["span_id"] for row in subtrees[0]} == {"root", "child"}


@pytest.mark.parametrize(
    "rows",
    [
        [
            _anchor_row("c" * 32, "root-1", "response-1"),
            _anchor_row("c" * 32, "root-2", "response-2"),
            _anchor_row(
                "c" * 32,
                "borrowed",
                "response-2",
                parent="root-1",
                operation_name="tool",
            ),
        ],
        [
            _anchor_row("c" * 32, "root-1", "response-1"),
            _anchor_row("c" * 32, "root-1", "response-2"),
        ],
        [
            _anchor_row(
                "c" * 32,
                "root-1",
                "response-1",
                parent="child",
            ),
            _anchor_row(
                "c" * 32,
                "child",
                "",
                parent="root-1",
                operation_name="chat",
            ),
        ],
        [
            _anchor_row(
                "c" * 32,
                "root-1",
                "response-1",
                agent_version="wrong",
            ),
            _anchor_row("c" * 32, "root-2", "response-2"),
        ],
    ],
)
def test_response_anchor_correlation_rejects_invalid_ancestry(rows) -> None:
    assert (
        _correlated_request_rows(
            rows,
            ("response-1", "response-2"),
            ("c" * 32, "c" * 32),
            agent_name="healthcare-agent-issue-010",
            foundry_version="7",
        )
        is None
    )


def test_response_anchor_ignores_unrelated_orphan_rows() -> None:
    operation_id = "c" * 32
    rows = [
        _anchor_row(operation_id, "root", "response-1"),
        _anchor_row(
            operation_id,
            "child",
            "",
            parent="root",
            operation_name="chat",
        ),
        _anchor_row(
            operation_id,
            "unrelated-orphan",
            "",
            parent="missing",
            operation_name="chat",
        ),
    ]
    correlation = _correlated_request_rows(
        rows,
        ("response-1",),
        (operation_id,),
        agent_name="healthcare-agent-issue-010",
        foundry_version="7",
    )
    assert correlation is not None
    subtrees, _ = correlation
    assert {row["span_id"] for row in subtrees[0]} == {"root", "child"}


def test_negative_argument_assertions_require_parsed_telemetry() -> None:
    omission_fixture = {
        "body": {"input": []},
        "trace_assertions": [
            {
                "name": "argument_omitted",
                "kind": "tool_argument_presence",
                "tool_name": "get_balance",
                "argument": "account_id",
                "present": False,
            }
        ],
    }
    missing_payload = _tool_trace_row("get_balance", ok=False)
    explicit_empty = _tool_trace_row(
        "get_balance",
        arguments={},
        ok=False,
    )
    assert _trace_assertion_result(
        [missing_payload],
        omission_fixture,
    )[0].passed is False
    assert _trace_assertion_result(
        [explicit_empty],
        omission_fixture,
    )[0].passed is True

    scope_fixture = {
        "body": {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Switch from trip-alpha to trip-beta.",
                        }
                    ],
                }
            ]
        },
        "trace_assertions": [
            {
                "name": "stale_scope",
                "kind": "scope_relation",
                "tool_name": "search_flights",
                "scope_kind": "trip",
                "request_scope": "last",
                "argument": "trip",
                "request_tool_equal": False,
            }
        ],
    }
    missing_scope = _tool_trace_row("search_flights", ok=True)
    stale_scope = _tool_trace_row(
        "search_flights",
        arguments={"trip": "trip-alpha"},
        ok=True,
    )
    assert _trace_assertion_result(
        [missing_scope],
        scope_fixture,
    )[0].passed is False
    assert _trace_assertion_result(
        [stale_scope],
        scope_fixture,
    )[0].passed is True


def test_trace_row_query_joins_split_reference_and_identity_spans_by_operation(
    monkeypatch,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    captured = []
    timespans = []

    class Table:
        rows = []

    class Result:
        status = "success"
        tables = [Table()]

    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    def query_resource(_client, query, **kwargs):
        captured.append(query)
        timespans.append(kwargs["timespan"])
        return Result()

    runtime._query_resource = query_resource  # type: ignore[method-assign]
    window_start = "2026-08-28T10:00:00+00:00"
    window_end = "2026-08-28T10:00:30+00:00"
    assert runtime._trace_rows(
        ("a" * 32,),
        ("response-reference",),
        "issue-013",
        "finance-agent",
        window_start,
        window_end,
    ) == []
    query = captured[0]
    assert 'customDimensions["gen_ai.tool.call.arguments"]' in query
    assert 'customDimensions["gen_ai.tool.call.result"]' in query
    assert query.index('customDimensions["gen_ai.response.id"]') < query.index(
        'customDimensions["azure.ai.agentserver.response_id"]'
    )
    assert query.index(
        'customDimensions["azure.ai.agentserver.response_id"]'
    ) < query.index('customDimensions["response_id"]')
    assert "request_id in" in query
    assert "matched_reference" in query
    assert 'set_has_element(agent_versions, "issue-013")' in query
    assert 'set_has_element(agent_names, "finance-agent")' in query
    assert "scoped_trace_operations" in query
    assert "operation_Id in (scoped_trace_operations)" in query
    outer = query.split("union traces, dependencies, requests", 2)[-1]
    assert (
        '| extend observed_agent=tostring(customDimensions["gen_ai.agent.name"])'
        in outer
    )
    assert (
        '| extend agent_version=tostring(customDimensions["gen_ai.agent.version"])'
        in outer
    )
    assert query.index("| summarize") < query.index(
        'set_has_element(agent_versions, "issue-013")'
    )
    assert timespans == [
        (
            datetime.fromisoformat(window_start),
            datetime.fromisoformat(window_end) + timedelta(minutes=15),
        )
    ]


def _trace_behavior_query_row(
    operation_id: str,
    reference: str,
    *,
    output_present: bool = True,
    output_nonempty: bool = True,
) -> list:
    return [
        operation_id,
        "span-id",
        "parent-span-id",
        "invoke_agent",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        '[{"role":"assistant","parts":[{"type":"text","content":"synthetic"}]}]',
        "2026-08-31T10:00:00+00:00",
        25.0,
        "invoke_agent",
        "true",
        "true",
        "false",
        reference,
        output_present,
        output_nonempty,
        "synthetic-agent",
        "1",
    ]


def test_trace_behavior_query_retries_transient_failure_and_uses_final_rows(
    monkeypatch,
) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    results = [
        types.SimpleNamespace(status=statuses.FAILURE, code="GatewayTimeout"),
        types.SimpleNamespace(
            status=statuses.SUCCESS,
            tables=[
                types.SimpleNamespace(
                    rows=[_trace_behavior_query_row(operation_id, reference)]
                )
            ],
        ),
    ]
    sleeps = []
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: results.pop(0)
    )
    runtime._sleep = sleeps.append

    rows = runtime._trace_rows((operation_id,))

    assert rows[0]["operation_id"] == operation_id
    assert rows[0]["matched_reference"] == reference
    assert sleeps == [1]
    assert results == []


def test_trace_behavior_query_retries_partial_result(monkeypatch) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    operation_id = "a" * 32
    results = [
        types.SimpleNamespace(status=statuses.PARTIAL, partial_data=[]),
        types.SimpleNamespace(
            status=statuses.SUCCESS,
            tables=[
                types.SimpleNamespace(
                    rows=[
                        _trace_behavior_query_row(
                            operation_id,
                            "resp_A1b2C3d4E5f6",
                        )
                    ]
                )
            ],
        ),
    ]
    sleeps = []
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: results.pop(0)
    )
    runtime._sleep = sleeps.append

    assert runtime._trace_rows((operation_id,))[0]["operation_id"] == operation_id
    assert sleeps == [1]
    assert results == []


def test_trace_behavior_query_rejects_permanent_failure_without_retry(
    monkeypatch,
) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    attempts = 0
    sleeps = []
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]

    def query(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return types.SimpleNamespace(
            status=statuses.FAILURE,
            code="BadArgument",
        )

    runtime._query_resource = query  # type: ignore[method-assign]
    runtime._sleep = sleeps.append

    with pytest.raises(
        TelemetryQueryError,
        match="rejected the trace behavior evidence query",
    ):
        runtime._trace_rows(("a" * 32,))

    assert attempts == 1
    assert sleeps == []


def test_trace_behavior_query_non_success_retries_are_bounded(monkeypatch) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    attempts = 0
    sleeps = []
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]

    def query(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return types.SimpleNamespace(
            status=statuses.FAILURE,
            code="GatewayTimeout",
        )

    runtime._query_resource = query  # type: ignore[method-assign]
    runtime._sleep = sleeps.append

    with pytest.raises(
        TelemetryQueryError,
        match="trace behavior evidence query retries were exhausted",
    ):
        runtime._trace_rows(("a" * 32,))

    assert attempts == 4
    assert sleeps == [1, 2, 4]


def test_trace_behavior_query_reuses_successful_rows_for_correlation_and_output(
    monkeypatch,
) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    attempts = 0
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]

    def query(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return types.SimpleNamespace(
            status=statuses.SUCCESS,
            tables=[
                types.SimpleNamespace(
                    rows=[_trace_behavior_query_row(operation_id, reference)]
                )
            ],
        )

    runtime._query_resource = query  # type: ignore[method-assign]

    rows = runtime._trace_rows((operation_id,))

    assert attempts == 1
    assert rows[0]["matched_reference"] == reference
    assert rows[0]["output_messages_present"] is True
    assert rows[0]["output_messages_nonempty"] is True


def test_collect_trace_evidence_emits_allowlisted_hashed_graph(monkeypatch) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    operation_id = "a" * 32
    captured = []

    class Table:
        rows = [
            [
                operation_id,
                "span-root",
                "upstream-server-span",
                "requests",
                "invoke_agent",
                "2026-08-31T10:00:00+00:00",
                25.0,
                True,
                "200",
                "",
                "",
                "",
                "",
                "true",
                "true",
                "false",
                False,
                False,
            ],
            [
                operation_id,
                "span-maf",
                "span-root",
                "dependencies",
                "invoke_agent",
                "2026-08-31T10:00:00.005+00:00",
                20.0,
                True,
                "",
                "",
                "",
                "",
                "",
                "true",
                "true",
                "false",
                True,
                True,
            ],
            [
                operation_id,
                "span-child",
                "span-maf",
                "dependencies",
                "execute_tool",
                "2026-08-31T10:00:00.010+00:00",
                10.0,
                False,
                0,
                "synthetic_lookup",
                "tool-call-private",
                "",
                "true",
                "",
                "",
                "false",
                True,
                True,
            ],
        ]

    class Result:
        status = "success"
        tables = [Table()]

    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda _client, query, **_kwargs: captured.append(query) or Result()
    )

    evidence = runtime.collect_trace_evidence((operation_id,))

    assert evidence["operation_count"] == 1
    assert evidence["span_count"] == 3
    operation = evidence["operations"][0]
    assert operation["operation_reference"].startswith("sha256:")
    assert operation_id not in json.dumps(evidence)
    root, inner, child = operation["spans"]
    assert root["sequence"] == 1
    assert root["operation_name"] == "invoke_agent"
    assert root["parent_span_reference"].startswith("sha256:")
    assert root["output_messages_present"] is False
    assert root["output_messages_nonempty"] is False
    assert inner["sequence"] == 2
    assert inner["telemetry_type"] == "dependencies"
    assert inner["operation_name"] == "invoke_agent"
    assert inner["output_messages_present"] is True
    assert inner["output_messages_nonempty"] is True
    assert inner["parent_span_reference"] == root["span_reference"]
    assert child["sequence"] == 3
    assert child["operation_name"] == "execute_tool"
    assert "output_messages_present" not in child
    assert "output_messages_nonempty" not in child
    assert child["tool_name"] == "synthetic_lookup"
    assert child["success"] == "False"
    assert child["result_code"] == "0"
    assert child["span_reference"].startswith("sha256:")
    assert child["parent_span_reference"] == inner["span_reference"]
    assert child["tool_call_reference"].startswith("sha256:")
    serialized = json.dumps(evidence)
    assert "span-root" not in serialized
    assert "span-maf" not in serialized
    assert "span-child" not in serialized
    assert "upstream-server-span" not in serialized
    assert "tool-call-private" not in serialized
    query = captured[0]
    assert "operation_Id in" in query
    assert 'customDimensions["gen_ai.response.id"]' not in query
    assert 'customDimensions["x-ms-client-request-id"]' not in query
    assert 'customDimensions["gen_ai.input.messages"]' not in query
    assert 'bag_has_key(\n    customDimensions, "gen_ai.output.messages")' in query
    assert (
        'isnotempty(tostring(customDimensions["gen_ai.output.messages"]))'
        in query
    )
    assert "output_messages_present, output_messages_nonempty" in query
    assert 'customDimensions["gen_ai.tool.call.arguments"]' not in query
    assert 'customDimensions["gen_ai.tool.call.result"]' not in query


def _trace_collection_row(operation_id: str) -> list:
    return [
        operation_id,
        "span-root",
        "upstream-server-span",
        "requests",
        "invoke_agent",
        "2026-08-31T10:00:00+00:00",
        25.0,
        True,
        "200",
        "",
        "",
        "",
        "",
        "true",
        "true",
        "false",
        True,
        True,
    ]


def test_trace_collection_retries_transient_non_success_result(monkeypatch) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    operation_id = "a" * 32
    results = [
        types.SimpleNamespace(status=statuses.FAILURE, code="GatewayTimeout"),
        types.SimpleNamespace(
            status=statuses.SUCCESS,
            tables=[types.SimpleNamespace(rows=[_trace_collection_row(operation_id)])],
        ),
    ]
    sleeps = []
    progress = []
    monotonic = iter((10.0, 12.0))
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: results.pop(0)
    )
    runtime._sleep = sleeps.append
    runtime._monotonic = lambda: next(monotonic)
    runtime.report_progress = progress.append  # type: ignore[method-assign]

    rows = runtime._collect_trace_rows((operation_id,))

    assert [row["operation_id"] for row in rows] == [operation_id]
    assert sleeps == [1]
    assert progress == [
        "Trace collection query returned failed result after 2s; "
        "retrying in 1s (2/4)"
    ]
    assert results == []


def test_trace_collection_retries_partial_result(monkeypatch) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    operation_id = "a" * 32
    results = [
        types.SimpleNamespace(status=statuses.PARTIAL, partial_data=[]),
        types.SimpleNamespace(
            status=statuses.SUCCESS,
            tables=[types.SimpleNamespace(rows=[_trace_collection_row(operation_id)])],
        ),
    ]
    sleeps = []
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: results.pop(0)
    )
    runtime._sleep = sleeps.append

    assert runtime._collect_trace_rows((operation_id,))[0]["operation_id"] == operation_id
    assert sleeps == [1]
    assert results == []


def test_trace_collection_non_success_retries_are_bounded(monkeypatch) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    attempts = 0
    sleeps = []
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]

    def query(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return types.SimpleNamespace(
            status=statuses.FAILURE,
            code="GatewayTimeout",
        )

    runtime._query_resource = query  # type: ignore[method-assign]
    runtime._sleep = sleeps.append

    with pytest.raises(
        TelemetryQueryError,
        match="trace collection query retries were exhausted",
    ):
        runtime._collect_trace_rows(("a" * 32,))

    assert attempts == 4
    assert sleeps == [1, 2, 4]


def test_trace_collection_rejects_permanent_invalid_query_without_retry(
    monkeypatch,
) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    attempts = 0
    sleeps = []
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]

    def query(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return types.SimpleNamespace(
            status=statuses.FAILURE,
            code="BadArgumentError",
        )

    runtime._query_resource = query  # type: ignore[method-assign]
    runtime._sleep = sleeps.append

    with pytest.raises(
        TelemetryQueryError,
        match="rejected the trace collection query",
    ):
        runtime._collect_trace_rows(("a" * 32,))

    assert attempts == 1
    assert sleeps == []


def test_trace_collection_reuses_successful_query_result(monkeypatch) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    operation_id = "a" * 32
    attempts = 0
    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]

    def query(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return types.SimpleNamespace(
            status=statuses.SUCCESS,
            tables=[types.SimpleNamespace(rows=[_trace_collection_row(operation_id)])],
        )

    runtime._query_resource = query  # type: ignore[method-assign]

    assert runtime._collect_trace_rows((operation_id,))[0]["operation_id"] == operation_id
    assert attempts == 1


def _canonical_output_messages_state(*rows) -> tuple[bool, bool]:
    operation_id = "a" * 32
    runtime = _runtime()
    runtime._collect_trace_rows = lambda _operations: [  # type: ignore[method-assign]
        {"operation_id": operation_id, **row} for row in rows
    ]
    return runtime.canonical_output_messages_state((operation_id,))[0]


def test_output_messages_accepts_inner_maf_invoke_agent_span() -> None:
    state = _canonical_output_messages_state(
        {
            "telemetry_type": "requests",
            "operation_name": "invoke_agent",
            "parent_span_id": "upstream-server-span",
            "output_messages_present": False,
            "output_messages_nonempty": False,
        },
        {
            "telemetry_type": "dependencies",
            "operation_name": "invoke_agent",
            "parent_span_id": "outer-request-span",
            "output_messages_present": True,
            "output_messages_nonempty": True,
        },
    )
    assert _canonical_output_messages_expectation_passes(
        state,
        expect_present=True,
    )


def test_output_messages_rejects_only_chat_child_with_output() -> None:
    state = _canonical_output_messages_state(
        {
            "telemetry_type": "requests",
            "operation_name": "invoke_agent",
            "parent_span_id": "upstream-server-span",
            "output_messages_present": False,
            "output_messages_nonempty": False,
        },
        {
            "telemetry_type": "dependencies",
            "operation_name": "chat",
            "parent_span_id": "outer-request-span",
            "output_messages_present": True,
            "output_messages_nonempty": True,
        },
    )
    assert not _canonical_output_messages_expectation_passes(
        state,
        expect_present=True,
    )


def test_output_messages_accepts_one_of_multiple_invoke_agent_spans() -> None:
    state = _canonical_output_messages_state(
        {
            "telemetry_type": "dependencies",
            "operation_name": "invoke_agent",
            "parent_span_id": "outer-request-span",
            "output_messages_present": False,
            "output_messages_nonempty": False,
        },
        {
            "telemetry_type": "dependencies",
            "operation_name": "invoke_agent",
            "parent_span_id": "outer-request-span",
            "output_messages_present": True,
            "output_messages_nonempty": False,
        },
        {
            "telemetry_type": "traces",
            "operation_name": "invoke_agent",
            "parent_span_id": "outer-request-span",
            "output_messages_present": True,
            "output_messages_nonempty": True,
        },
    )
    assert _canonical_output_messages_expectation_passes(
        state,
        expect_present=True,
    )


def test_output_messages_rejects_trace_without_invoke_agent_span() -> None:
    with pytest.raises(ContractError, match="missing an invoke_agent span"):
        _canonical_output_messages_state(
            {
                "telemetry_type": "dependencies",
                "operation_name": "chat",
                "parent_span_id": "outer-request-span",
                "output_messages_present": True,
                "output_messages_nonempty": True,
            }
        )


def test_expected_absent_requires_no_invoke_agent_attribute() -> None:
    absent_state = _canonical_output_messages_state(
        {
            "telemetry_type": "dependencies",
            "operation_name": "invoke_agent",
            "parent_span_id": "outer-request-span",
            "output_messages_present": False,
            "output_messages_nonempty": False,
        }
    )
    present_state = _canonical_output_messages_state(
        {
            "telemetry_type": "dependencies",
            "operation_name": "invoke_agent",
            "parent_span_id": "outer-request-span",
            "output_messages_present": True,
            "output_messages_nonempty": False,
        }
    )
    assert _canonical_output_messages_expectation_passes(
        absent_state,
        expect_present=False,
    )
    assert not _canonical_output_messages_expectation_passes(
        present_state,
        expect_present=False,
    )


def test_prompt_request_invoke_agent_output_passes() -> None:
    state = _canonical_output_messages_state(
        {
            "telemetry_type": "requests",
            "operation_name": "invoke_agent",
            "parent_span_id": "upstream-server-span",
            "output_messages_present": True,
            "output_messages_nonempty": True,
        }
    )
    assert _canonical_output_messages_expectation_passes(
        state,
        expect_present=True,
    )


def test_support_internal_invoke_agent_output_passes() -> None:
    state = _canonical_output_messages_state(
        {
            "telemetry_type": "dependencies",
            "operation_name": "invoke_agent",
            "parent_span_id": "server-request-span",
            "output_messages_present": True,
            "output_messages_nonempty": True,
        }
    )
    assert _canonical_output_messages_expectation_passes(
        state,
        expect_present=True,
    )


def test_collect_trace_evidence_rejects_incomplete_operation_set() -> None:
    runtime = _runtime()
    operation_a = "a" * 32
    operation_b = "b" * 32
    runtime._collect_trace_rows = lambda _operations: [  # type: ignore[method-assign]
        {"operation_id": operation_a}
    ]

    with pytest.raises(ContractError, match="incomplete"):
        runtime.collect_trace_evidence((operation_a, operation_b))


def _write_trace_assertion_traffic(
    path: Path,
    request_count: int = 1,
    *,
    with_trace_assertions: bool = True,
) -> None:
    expected = {"http_status": 200}
    if with_trace_assertions:
        expected["trace_assertions"] = [
            {
                "name": "one_lookup",
                "kind": "tool_call_count",
                "tool_name": "lookup",
                "count": 1,
            }
        ]
    path.write_text(
        json.dumps(
            {
                "requests": [
                    {
                        "id": f"request_A1b2C3d4_{index}",
                        "request": {
                            "body": {
                                "input": "synthetic request",
                                "conversation": {
                                    "id": f"conversation_A1b2C3d4_{index}"
                                },
                            }
                        },
                        "expected": expected,
                    }
                    for index in range(request_count)
                ]
            }
        ),
        encoding="utf-8",
    )


def _invoke_agent_trace_row(
    reference: str,
    *,
    present: bool,
    nonempty: bool,
    telemetry_type: str = "requests",
) -> dict:
    row = _tool_trace_row("")
    row.update(
        {
            "operation_name": "invoke_agent",
            "matched_reference": reference,
            "telemetry_type": telemetry_type,
            "output_messages_present": present,
            "output_messages_nonempty": nonempty,
            "span_id": "root-span",
            "parent_span_id": "",
            "agent_name": "finance-agent",
            "agent_version": "v0",
        }
    )
    return row


def _anchored_tool_rows(
    tool_name: str,
    reference: str,
    *,
    operation_id: str = "a" * 32,
    agent_name: str = "finance-agent",
    agent_version: str = "issue-013",
) -> list[dict]:
    return [
        _anchor_row(
            operation_id,
            "root-span",
            reference,
            agent_name=agent_name,
            agent_version=agent_version,
        ),
        {
            **_tool_trace_row(tool_name),
            "operation_id": operation_id,
            "span_id": "tool-span",
            "parent_span_id": "root-span",
        },
    ]


def test_trace_assertion_correlation_fails_immediately_when_ambiguous(
    tmp_path,
) -> None:
    monotonic = [0.0]
    sleeps = []
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_a = "a" * 32
    operation_b = "b" * 32
    reference_a = "resp_A1b2C3d4E5f6"
    reference_b = "resp_F6e5D4c3B2a1"
    rows = [
        {
            **_tool_trace_row("lookup"),
            "operation_id": operation_a,
            "matched_reference": reference_a,
        },
        {
            **_tool_trace_row("lookup"),
            "operation_id": operation_b,
            "matched_reference": reference_a,
        },
        {
            **_tool_trace_row("lookup"),
            "operation_id": operation_b,
            "matched_reference": reference_b,
        },
    ]
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path, request_count=2)
    runtime._trace_rows = lambda *_args: rows  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    first_passes = []

    with pytest.raises(ContractError, match="ambiguous"):
        runtime.trace_assertion_evidence(
            agent_name="finance-agent",
            foundry_version="issue-013",
            operation_ids=(operation_a, operation_b),
            response_references=(reference_a, reference_b),
            window_start="2026-08-28T10:00:00+00:00",
            window_end="2026-08-28T10:00:30+00:00",
            traffic_path=traffic_path,
            stabilization_seconds=180,
            on_first_pass=lambda: first_passes.append(monotonic[0]),
        )
    assert sleeps == []
    assert first_passes == []


def test_trace_assertion_waits_for_invoke_agent_hydration(
    tmp_path,
    monkeypatch,
) -> None:
    statuses = _install_logs_query_status(monkeypatch)
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    correlated = [
        operation_id,
        "tool-span",
        "root-span",
        "execute_tool",
        "lookup",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "2026-08-28T10:00:00+00:00",
        10.0,
        "tool.lookup",
        "",
        "",
        "",
        reference,
        False,
        False,
        "",
        "",
    ]
    hydrated = [
        operation_id,
        "root-span",
        "",
        "invoke_agent",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        '[{"role":"assistant","parts":[{"type":"text","content":"synthetic"}]}]',
        "2026-08-28T10:00:01+00:00",
        25.0,
        "invoke_agent",
        "",
        "",
        "",
        reference,
        True,
        True,
        "finance-agent",
        "issue-013",
    ]
    polls = []
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]

    def query(*_args, **_kwargs):
        polls.append(monotonic[0])
        rows = [correlated] if monotonic[0] < 135 else [correlated, hydrated]
        return types.SimpleNamespace(
            status=statuses.SUCCESS,
            tables=[types.SimpleNamespace(rows=rows)],
        )

    runtime._query_resource = query  # type: ignore[method-assign]
    runtime._collect_trace_rows = (  # type: ignore[method-assign]
        lambda *_args: pytest.fail("Stable trace rows must be reused")
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    states = []

    evidence = runtime.trace_assertion_evidence(
        agent_name="finance-agent",
        foundry_version="issue-013",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: None,
        on_stable_output_messages=states.append,
    )

    assert evidence[0][0].passed is True
    assert states == [((True, True),)]
    assert monotonic[0] == 315
    assert polls[0] == 0
    assert polls[-1] == 315


def test_trace_assertion_waits_for_inner_maf_output_hydration(tmp_path) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    outer = _invoke_agent_trace_row(
        reference,
        present=False,
        nonempty=False,
    )
    inner = _invoke_agent_trace_row(
        reference,
        present=True,
        nonempty=True,
        telemetry_type="dependencies",
    )
    inner.update(
        {
            "span_id": "inner-span",
            "parent_span_id": "root-span",
            "matched_reference": "",
        }
    )
    runtime._trace_rows = (  # type: ignore[method-assign]
        lambda *_args: [outer] if monotonic[0] < 135 else [outer, inner]
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path, with_trace_assertions=False)
    states = []

    runtime.trace_assertion_evidence(
        agent_name="finance-agent",
        foundry_version="v0",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: None,
        on_stable_output_messages=states.append,
    )

    assert states == [((True, True),)]
    assert monotonic[0] == 315


@pytest.mark.parametrize("state", [(False, False), (True, False)])
def test_trace_assertion_returns_stable_empty_output_state(
    tmp_path,
    state,
) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    rows = [
        _invoke_agent_trace_row(
            reference,
            present=state[0],
            nonempty=state[1],
        )
    ]
    runtime._trace_rows = lambda *_args: rows  # type: ignore[method-assign]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path, with_trace_assertions=False)
    states = []

    runtime.trace_assertion_evidence(
        agent_name="finance-agent",
        foundry_version="v0",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: None,
        on_stable_output_messages=states.append,
    )

    assert states == [(state,)]
    assert monotonic[0] == 180


def test_trace_assertion_bounds_missing_invoke_agent_hydration(tmp_path) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    rows = [
        {
            **_tool_trace_row("lookup"),
            "operation_id": operation_id,
            "matched_reference": reference,
        }
    ]
    runtime._trace_rows = lambda *_args: rows  # type: ignore[method-assign]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)

    with pytest.raises(
        TraceAssertionActivationError,
        match="exact response-to-operation correlation",
    ):
        runtime.trace_assertion_evidence(
            agent_name="finance-agent",
            foundry_version="issue-013",
            operation_ids=(operation_id,),
            response_references=(reference,),
            window_start="2026-08-28T10:00:00+00:00",
            window_end="2026-08-28T10:00:30+00:00",
            traffic_path=traffic_path,
            stabilization_seconds=180,
            on_first_pass=lambda: None,
            on_stable_output_messages=lambda _states: None,
        )

    assert monotonic[0] == 15 * 60


def test_trace_assertion_stable_failure_waits_for_deadline(
    tmp_path,
) -> None:
    monotonic = [0.0]
    poll_times = []
    progress = []
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    rows = [
        _anchor_row(
            operation_id,
            "root-span",
            reference,
            agent_name="finance-agent",
            agent_version="issue-013",
        ),
        {
            **_tool_trace_row("different_lookup"),
            "operation_id": operation_id,
            "span_id": "tool-span",
            "parent_span_id": "root-span",
        }
    ]
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    runtime._trace_rows = (  # type: ignore[method-assign]
        lambda *_args: poll_times.append(monotonic[0]) or rows
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    runtime.report_progress = progress.append  # type: ignore[method-assign]
    first_passes = []

    evidence = runtime.trace_assertion_evidence(
        agent_name="finance-agent",
        foundry_version="issue-013",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: first_passes.append(monotonic[0]),
    )

    assert evidence[0][0].passed is False
    assert monotonic[0] == 15 * 60
    assert poll_times[-1] == 15 * 60
    assert progress
    assert "failing evidence is stabilizing" in progress[-1]
    assert first_passes == [0]


def test_trace_assertion_observes_span_ingested_after_135_seconds(
    tmp_path,
) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    incomplete = _anchored_tool_rows(
        "different_lookup",
        reference,
        operation_id=operation_id,
    )
    complete = _anchored_tool_rows(
        "lookup",
        reference,
        operation_id=operation_id,
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    runtime._trace_rows = (  # type: ignore[method-assign]
        lambda *_args: incomplete if monotonic[0] < 135 else complete
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    first_passes = []

    evidence = runtime.trace_assertion_evidence(
        agent_name="finance-agent",
        foundry_version="issue-013",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: first_passes.append(monotonic[0]),
    )

    assert evidence[0][0].passed is True
    assert monotonic[0] == 135 + 180
    assert first_passes == [0]


def test_trace_assertion_late_duplicate_invalidates_stabilizing_pass(
    tmp_path,
) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    first = _anchored_tool_rows(
        "lookup",
        reference,
        operation_id=operation_id,
    )
    duplicate = {
        **first[1],
        "span_id": "late-tool-span",
        "timestamp": "2026-08-28T10:00:01+00:00",
    }
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    runtime._trace_rows = (  # type: ignore[method-assign]
        lambda *_args: first if monotonic[0] < 135 else [*first, duplicate]
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    first_passes = []

    evidence = runtime.trace_assertion_evidence(
        agent_name="finance-agent",
        foundry_version="issue-013",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: first_passes.append(monotonic[0]),
    )

    assert evidence[0][0].passed is False
    assert monotonic[0] == 15 * 60
    assert first_passes == [0]


def test_trace_assertion_late_external_operation_is_ambiguous(
    tmp_path,
) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    target = _anchored_tool_rows(
        "lookup",
        reference,
        operation_id=operation_id,
    )
    external = {
        **target[0],
        "operation_id": "b" * 32,
        "span_id": "external-root",
        "timestamp": "2026-08-28T10:00:01+00:00",
    }
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    runtime._trace_rows = (  # type: ignore[method-assign]
        lambda *_args: target if monotonic[0] < 135 else [*target, external]
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    first_passes = []

    with pytest.raises(ContractError, match="ambiguous"):
        runtime.trace_assertion_evidence(
            agent_name="finance-agent",
            foundry_version="issue-013",
            operation_ids=(operation_id,),
            response_references=(reference,),
            window_start="2026-08-28T10:00:00+00:00",
            window_end="2026-08-28T10:00:30+00:00",
            traffic_path=traffic_path,
            stabilization_seconds=180,
            on_first_pass=lambda: first_passes.append(monotonic[0]),
        )

    assert monotonic[0] == 135
    assert first_passes == [0]


def test_trace_assertion_stable_pass_waits_for_ingestion_interval(
    tmp_path,
) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    rows = _anchored_tool_rows(
        "lookup",
        reference,
        operation_id=operation_id,
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    runtime._trace_rows = lambda *_args: rows  # type: ignore[method-assign]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    first_passes = []

    evidence = runtime.trace_assertion_evidence(
        agent_name="finance-agent",
        foundry_version="issue-013",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: first_passes.append(monotonic[0]),
    )

    assert evidence[0][0].passed is True
    assert monotonic[0] == 180
    assert first_passes == [0]


def test_hosted_correlation_without_assertions_waits_for_ingestion_interval(
    tmp_path,
) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    rows = _anchored_tool_rows(
        "lookup",
        reference,
        operation_id=operation_id,
        agent_name="travel-agent",
        agent_version="issue-021",
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(
        traffic_path,
        with_trace_assertions=False,
    )
    runtime._trace_rows = lambda *_args: rows  # type: ignore[method-assign]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    first_mappings = []

    evidence = runtime.trace_assertion_evidence(
        agent_name="travel-agent",
        foundry_version="issue-021",
        operation_ids=(operation_id,),
        response_references=(reference,),
        window_start="2026-08-28T10:00:00+00:00",
        window_end="2026-08-28T10:00:30+00:00",
        traffic_path=traffic_path,
        stabilization_seconds=180,
        on_first_pass=lambda: first_mappings.append(monotonic[0]),
    )

    assert evidence == ((),)
    assert monotonic[0] == 180
    assert first_mappings == [0]


def test_trace_assertion_requires_correlation_in_final_snapshot(
    tmp_path,
) -> None:
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    rows = _anchored_tool_rows(
        "different_lookup",
        reference,
        operation_id=operation_id,
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    runtime._trace_rows = (  # type: ignore[method-assign]
        lambda *_args: rows if monotonic[0] < 15 * 60 else []
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )

    first_mappings = []
    with pytest.raises(ContractError, match="exact response-to-operation correlation"):
        runtime.trace_assertion_evidence(
            agent_name="finance-agent",
            foundry_version="issue-013",
            operation_ids=(operation_id,),
            response_references=(reference,),
            window_start="2026-08-28T10:00:00+00:00",
            window_end="2026-08-28T10:00:30+00:00",
            traffic_path=traffic_path,
            stabilization_seconds=180,
            on_first_pass=lambda: first_mappings.append(monotonic[0]),
        )
    assert monotonic[0] == 15 * 60
    assert first_mappings == [0]


def test_trace_assertion_rejects_pass_first_seen_near_deadline(
    tmp_path,
) -> None:
    monotonic = [0.0]
    first_passes = []
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        monotonic=lambda: monotonic[0],
    )
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    failing = _anchored_tool_rows(
        "different_lookup",
        reference,
        operation_id=operation_id,
    )
    passing = _anchored_tool_rows(
        "lookup",
        reference,
        operation_id=operation_id,
    )
    traffic_path = tmp_path / "traffic.json"
    _write_trace_assertion_traffic(traffic_path)
    runtime._trace_rows = (  # type: ignore[method-assign]
        lambda *_args: failing if monotonic[0] < 885 else passing
    )
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )

    with pytest.raises(ContractError, match="did not stabilize"):
        runtime.trace_assertion_evidence(
            agent_name="finance-agent",
            foundry_version="issue-013",
            operation_ids=(operation_id,),
            response_references=(reference,),
            window_start="2026-08-28T10:00:00+00:00",
            window_end="2026-08-28T10:00:30+00:00",
            traffic_path=traffic_path,
            stabilization_seconds=180,
            on_first_pass=lambda: first_passes.append(monotonic[0]),
        )
    assert monotonic[0] == 15 * 60
    assert first_passes == [0]


def test_trace_assertions_cover_payload_cardinality_and_span_order() -> None:
    first = _tool_trace_row(
        "search_flights",
        arguments={"trip": "trip-alpha"},
        result={"result_count": 80},
        ok=True,
        timestamp="2026-08-28T10:00:00+00:00",
        duration=10.0,
    )
    second = _tool_trace_row(
        "search_hotels",
        result={"result_count": 80},
        ok=True,
        timestamp="2026-08-28T10:00:00.020+00:00",
        duration=10.0,
    )
    fixture = {
        "body": {"input": []},
        "trace_assertions": [
            {
                "name": "trip_argument_present",
                "kind": "tool_argument_presence",
                "tool_name": "search_flights",
                "argument": "trip",
                "present": True,
            },
            {
                "name": "expanded_inventory_payload",
                "kind": "payload_multiplicity",
                "source": "tool_result",
                "tool_name": "search_flights",
                "path": "result_count",
                "minimum": 80,
                "maximum": 80,
            },
            {
                "name": "searches_are_ordered",
                "kind": "span_relation",
                "first_tool": "search_flights",
                "second_tool": "search_hotels",
                "relation": "ordered",
            },
        ],
    }
    results = _trace_assertion_result([first, second], fixture)
    assert all(item.passed for item in results)
    overlapping = {
        **second,
        "timestamp": "2026-08-28T10:00:00.005+00:00",
    }
    results = _trace_assertion_result([first, overlapping], fixture)
    assert results[0].passed is True
    assert results[1].passed is True
    assert results[2].passed is False
    overlap_fixture = {
        **fixture,
        "trace_assertions": [
            {
                "name": "searches_overlap",
                "kind": "span_relation",
                "first_tool": "search_flights",
                "second_tool": "search_hotels",
                "relation": "overlap",
            }
        ],
    }
    assert _trace_assertion_result(
        [first, overlapping],
        overlap_fixture,
    )[0].passed is True


def test_travel_028_trace_scope_rejects_latest_destination() -> None:
    request = read_json(
        ROOT
        / "agents"
        / "travel-agent"
        / "issues"
        / "issue-028"
        / "traffic.json"
    )["requests"][0]
    fixture = _normalize_fixture(request)
    stale = _tool_trace_row(
        "search_flights",
        arguments={"trip": "trip-alpha"},
        result={"result_count": 2},
        ok=True,
    )
    latest = _tool_trace_row(
        "search_flights",
        arguments={"trip": "trip-beta"},
        result={"result_count": 2},
        ok=True,
    )
    assert all(
        item.passed for item in _trace_assertion_result([stale], fixture)
    )
    assert any(
        not item.passed for item in _trace_assertion_result([latest], fixture)
    )


def test_trace_behavior_summary_records_hosted_tool_recovery() -> None:
    summary = _trace_behavior_summary(
        [
            {
                "operation_id": "a" * 32,
                "operation_name": "execute_tool",
                "tool_name": "read_ticket",
                "tool_call_id": "",
                "error_type": "",
                "tool_ok": "false",
                "tool_result": "",
                "messages": ["", ""],
            },
            {
                "operation_id": "a" * 32,
                "operation_name": "execute_tool",
                "tool_name": "read_ticket",
                "tool_call_id": "",
                "error_type": "",
                "tool_ok": "true",
                "tool_result": "",
                "messages": ["", ""],
            },
        ]
    )
    assert summary["tool_call_counts"] == {"read_ticket": 2}
    assert summary["successful_tool_response_count"] == 1
    assert summary["error_codes"] == {"tool_error": 1}


def test_trace_behavior_summary_records_hosted_terminal_and_handled_error() -> None:
    operation_id = "a" * 32
    summary = _trace_behavior_summary(
        [
            {
                "operation_id": operation_id,
                "operation_name": "execute_tool",
                "tool_name": "read_ticket",
                "tool_call_id": "call-1",
                "error_type": "synthetic_optional_history_failure",
                "tool_ok": "false",
                "tool_result": "",
                "messages": ["", ""],
                "handled_error": "true",
                "terminal_success": "",
                "terminal_output": "",
            },
            {
                "operation_id": operation_id,
                "operation_name": "invoke_agent",
                "tool_name": "",
                "tool_call_id": "",
                "error_type": "",
                "tool_ok": "",
                "tool_result": "",
                "messages": ["", ""],
                "handled_error": "",
                "terminal_success": "true",
                "terminal_output": "true",
            },
        ]
    )
    assert summary["handled_error_count"] == 1
    assert summary["unhandled_error_count"] == 0
    assert summary["terminal_response_count"] == 1
    assert summary["explicit_terminal_success_count"] == 1
    assert summary["explicit_terminal_output_count"] == 1
    assert summary["terminal_success_count"] == 1
    assert summary["terminal_output_count"] == 1


def test_trace_behavior_summary_requires_terminal_visible_response() -> None:
    messages = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": "Checking now."}],
            },
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "lookup_slots",
                    }
                ],
            },
            {
                "role": "tool",
                "parts": [
                    {
                        "type": "tool_call_response",
                        "response": {"slots": ["slot-demo-1"]},
                    }
                ],
            },
        ]
    )
    row = {
        "operation_id": "a" * 32,
        "operation_name": "invoke_agent",
        "tool_name": "",
        "tool_call_id": "",
        "error_type": "",
        "tool_ok": "",
        "tool_result": "",
        "messages": [messages, ""],
    }
    assert _trace_behavior_summary([row])["assistant_response_count"] == 0
    with_final = json.loads(messages)
    with_final.append(
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "One slot is available."}],
        }
    )
    row["messages"] = ["", json.dumps(with_final)]
    assert _trace_behavior_summary([row])["assistant_response_count"] == 1
    with_final.append(
        {
            "role": "user",
            "parts": [{"type": "text", "content": "One more question."}],
        }
    )
    row["messages"] = ["", json.dumps(with_final)]
    assert _trace_behavior_summary([row])["assistant_response_count"] == 0
    terminal = with_final[:-1]
    first = {**row, "operation_id": "a" * 32, "messages": ["", json.dumps(terminal)]}
    second = {**row, "operation_id": "b" * 32, "messages": ["", json.dumps(terminal)]}
    assert (
        _trace_behavior_summary([first, second])["assistant_response_count"]
        == 2
    )
    text_and_tool = [
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "Checking now."},
                {"type": "tool_call", "id": "call-2", "name": "lookup_slots"},
            ],
        }
    ]
    row["messages"] = ["", json.dumps(text_and_tool)]
    assert _trace_behavior_summary([row])["assistant_response_count"] == 0
    alternate = [
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "A different final answer."}],
        }
    ]
    first = {
        **row,
        "messages": ["", json.dumps(terminal)],
        "timestamp": "2026-08-26T10:01:00Z",
    }
    second = {
        **row,
        "messages": ["", json.dumps(alternate)],
        "timestamp": "2026-08-26T10:02:00Z",
    }
    assert _trace_behavior_summary([first, second])["assistant_response_count"] == 1
    assert _trace_behavior_summary([second, first])["assistant_response_count"] == 1


def test_trace_behavior_summary_deduplicates_tool_call_ids() -> None:
    messages = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "lookup_slots",
                    }
                ],
            }
        ]
    )
    summary = _trace_behavior_summary(
        [
            {
                "operation_id": "a" * 32,
                "operation_name": "execute_tool",
                "tool_name": "unknown",
                "tool_call_id": "call-1",
                "error_type": "",
                "tool_ok": "true",
                "tool_result": "",
                "messages": [messages, ""],
            }
        ]
    )
    assert summary["tool_call_counts"] == {"lookup_slots": 1}


def test_trace_behavior_summary_deduplicates_tool_responses() -> None:
    messages = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool_call",
                        "id": "call-1",
                        "name": "lookup_slots",
                    }
                ],
            },
            {
                "role": "tool",
                "parts": [
                    {
                        "type": "tool_call_response",
                        "id": "call-1",
                        "response": {
                            "error": {"code": "temporary_unavailable"}
                        },
                    }
                ],
            },
        ]
    )
    result = json.dumps(
        {"error": {"code": "temporary_unavailable"}}
    )
    summary = _trace_behavior_summary(
        [
            {
                "operation_id": "a" * 32,
                "operation_name": "execute_tool",
                "tool_name": "lookup_slots",
                "tool_call_id": "call-1",
                "error_type": "",
                "tool_ok": "",
                "tool_result": result,
                "messages": [messages, ""],
            }
        ]
    )
    assert summary["tool_call_counts"] == {"lookup_slots": 1}
    assert summary["tool_response_count"] == 1
    assert summary["error_codes"] == {"temporary_unavailable": 1}


def test_tool_fixtures_fail_closed() -> None:
    with pytest.raises(ContractError, match="cannot contain tool fixtures"):
        _normalize_fixture(
            {
                "id": "health-baseline",
                "request": {"body": {"input": "synthetic lookup"}},
                "tool_fixtures": [],
            }
        )


def test_vacuous_semantic_assertions_fail_closed() -> None:
    with pytest.raises(ContractError, match="exact JSON"):
        _normalize_fixture(
            {
                "id": "weather-baseline",
                "request": {"body": {"input": "synthetic evidence"}},
                "expected": {
                    "semantic_assertions": {"exact_json_fields": {}}
                },
            }
        )
    with pytest.raises(ContractError, match="JSON schema"):
        _normalize_fixture(
            {
                "id": "weather-baseline",
                "request": {"body": {"input": "synthetic evidence"}},
                "expected": {"semantic_assertions": {"json_schema": {}}},
            }
        )
    with pytest.raises(ContractError, match="casefold JSON"):
        _normalize_fixture(
            {
                "id": "weather-baseline",
                "request": {"body": {"input": "synthetic evidence"}},
                "expected": {
                    "semantic_assertions": {
                        "casefold_json_fields": {"temperature": 21}
                    }
                },
            }
        )


def test_prompt_runtime_rejects_unsupported_text_format() -> None:
    runtime = _runtime()
    with pytest.raises(ContractError, match="unsupported request-side text formatting"):
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {
                    "input": "synthetic evidence",
                    "text": {"format": {"type": "json_schema"}},
                },
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            1,
            None,
        )


def test_prompt_function_call_fails_closed() -> None:
    runtime = _runtime()
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {"mark_started": staticmethod(lambda *_args, **_kwargs: None)},
    )()
    runtime._json_request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "id": "synthetic-response",
        "output": [
            {
                "type": "function_call",
                "call_id": "synthetic-call",
                "name": "unsupported",
                "arguments": "{}",
            }
        ],
    }
    with pytest.raises(ContractError, match="pure Prompt"):
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {"input": "Synthetic request"},
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            1,
            None,
        )


def test_prompt_json_contract_is_not_forwarded_to_endpoint() -> None:
    runtime = _runtime()
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {"mark_started": staticmethod(lambda *_args, **_kwargs: None)},
    )()
    request = read_json(
        ROOT / "agents" / "weather-agent" / "v0" / "traffic.json"
    )["requests"][3]
    fixture = _normalize_fixture(request)
    captured: dict[str, object] = {}

    def json_request(method, url, body, **kwargs):
        captured.update(
            {
                "method": method,
                "url": url,
                "body": body,
                "kwargs": kwargs,
            }
        )
        return {
            "id": "synthetic-response",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"condition":"clear","temperature":21,'
                                '"unit":"celsius"}'
                            ),
                        }
                    ],
                }
            ],
        }

    runtime._json_request = json_request  # type: ignore[method-assign]
    result = runtime._invoke_prompt(
        "weather-agent",
        "1",
        fixture,
        7,
        None,
    )

    forwarded = captured["body"]
    assert isinstance(forwarded, dict)
    assert "text" not in forwarded
    assert "conversation" not in forwarded
    assert forwarded["store"] is True
    assert not {
        "function_call",
        "functions",
        "parallel_tool_calls",
        "tool",
        "tool_choice",
        "tools",
    }.intersection(forwarded)
    assert result[1] is True
    assert result[2] == result[3] == 4
    assert result[5] == 0


def test_prompt_group_uses_previous_response_only_for_next_memory_turn() -> None:
    runtime = _runtime()
    previous_values = []

    def invoke_prompt(
        _agent_name,
        _foundry_version,
        fixture,
        _seed,
        previous_response_id,
    ):
        previous_values.append(previous_response_id)
        index = fixture["_index"]
        return (
            [f"response-{index}"],
            True,
            1,
            1,
            1,
            0,
            (),
            True,
        )

    runtime._invoke_prompt = invoke_prompt  # type: ignore[method-assign]
    runtime._invoke_group(
        "weather-agent",
        "prompt",
        "1",
        [
            {"_index": 0},
            {"_index": 1},
        ],
        7,
    )
    assert previous_values == [None, "response-0"]


def test_five_prompt_requests_map_to_five_direct_executions(tmp_path: Path) -> None:
    runtime = _runtime()
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {
            "mark_started": staticmethod(lambda *_args, **_kwargs: None),
            "mark_completed": staticmethod(lambda *_args, **_kwargs: None),
        },
    )()

    def invoke_prompt(
        _agent_name,
        _foundry_version,
        fixture,
        _seed,
        previous_response_id,
    ):
        assert previous_response_id is None
        index = fixture["_index"]
        return ([f"response-{index}"], True, 1, 1, 1, 0, (), False)

    runtime._invoke_prompt = invoke_prompt  # type: ignore[method-assign]
    traffic = tmp_path / "traffic.json"
    traffic.write_text(
        json.dumps(
            {
                "requests": [
                    {
                        "id": f"request-{index}",
                        "request": {
                            "body": {"input": f"Synthetic evidence {index}"}
                        },
                        "expected": {
                            "semantic_assertions": {
                                "required_claims": ["synthetic"]
                            }
                        },
                    }
                    for index in range(5)
                ]
            }
        ),
        encoding="utf-8",
    )
    evidence = runtime.invoke_version(
        agent_name="weather-agent",
        agent_type="prompt",
        foundry_version="1",
        traffic_path=traffic,
        seed=11,
    )
    assert evidence.request_count == evidence.response_count == 5
    assert len(evidence.response_references) == 5
    assert all(
        item.response_count == item.direct_terminal_response_count == 1
        and item.function_call_count == 0
        for item in evidence.request_summaries
    )


def test_insight_parser_supports_paged_wire_trace_details() -> None:
    runtime = _runtime()
    insight = runtime._to_insight(
        {
            "id": "private-id",
            "agentVersion": "9",
            "title": "Synthetic defect",
            "description": "One deterministic defect.",
            "category": "reliability_errors",
            "severity": "high",
            "updatedAt": "2026-08-24T10:00:00Z",
            "details": {
                "highlightedTraces": [
                    {"traceId": "a" * 32},
                ],
                "linkedTraces": [
                    {"traceId": "a" * 32},
                    {"traceId": "b" * 32},
                ],
                "recommendedActions": {
                    "proposedFix": {"text": "Apply the bounded correction."}
                },
            },
        }
    )
    assert insight.agent_version == "9"
    assert insight.trace_count == 2
    assert insight.linked_operation_ids == ("a" * 32, "b" * 32)
    assert insight.proposed_fix == "Apply the bounded correction."


def test_finished_insights_exclude_cards_linked_to_foreign_operations() -> None:
    runtime = _runtime()
    target_operation = "a" * 32
    foreign_operation = "b" * 32
    earliest = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    latest = earliest + timedelta(seconds=30)
    runtime._wait_insights_run = (  # type: ignore[method-assign]
        lambda *_args: {
            "status": "succeeded",
            "window_start": (earliest - timedelta(seconds=1)).isoformat(),
            "window_end": (latest + timedelta(seconds=1)).isoformat(),
        }
    )
    runtime._operation_time_bounds = (  # type: ignore[method-assign]
        lambda **_kwargs: (earliest, latest)
    )

    def insight(
        reference: str,
        linked_operation_ids: tuple[str, ...],
    ) -> dict:
        return {
            "id": reference,
            "agentVersion": "issue-013",
            "title": reference,
            "updatedAt": "2026-08-24T10:01:00+00:00",
            "details": {
                "linkedTraces": [
                    {"traceId": operation_id}
                    for operation_id in linked_operation_ids
                ]
            },
        }

    runtime._list_insights = lambda _monitor_id: [  # type: ignore[method-assign]
        insight("target-only", (target_operation,)),
        insight("mixed", (target_operation, foreign_operation)),
        insight("foreign-only", (foreign_operation,)),
        insight("unlinked", ()),
    ]

    result = runtime.finish_insights_run(
        agent_name="finance-agent",
        monitor_id="monitor-finance",
        foundry_version="issue-013",
        operation_ids=(target_operation,),
        checkpoint=InsightRunCheckpoint("private-run-id", {}),
    )

    assert [item.title for item in result.insights] == ["target-only"]


def test_telemetry_requires_every_request_reference() -> None:
    class Table:
        rows = [
            ["a" * 32, ["response-1"]],
            ["b" * 32, ["response-2"]],
        ]

    assert _complete_operation_ids([Table()], ("response-1", "response-2")) == (
        "a" * 32,
        "b" * 32,
    )
    assert _complete_operation_ids(
        [Table()],
        ("response-1", "response-2", "response-3"),
    ) is None

    class AmbiguousTable:
        rows = [
            ["a" * 32, ["resp_A1b2C3d4E5f6"]],
            ["b" * 32, ["resp_A1b2C3d4E5f6"]],
        ]

    assert _complete_operation_ids(
        [AmbiguousTable()],
        ("resp_A1b2C3d4E5f6",),
    ) is None

    class SharedOperationTable:
        rows = [
            [
                "a" * 32,
                ["resp_A1b2C3d4E5f6", "resp_F6e5D4c3B2a1"],
            ]
        ]

    assert _complete_operation_ids(
        [SharedOperationTable()],
        ("resp_A1b2C3d4E5f6", "resp_F6e5D4c3B2a1"),
        allow_shared_operations=True,
    ) == ("a" * 32, "a" * 32)
    assert (
        _complete_operation_ids(
            [SharedOperationTable()],
            ("resp_A1b2C3d4E5f6", "resp_F6e5D4c3B2a1"),
        )
        is None
    )


@pytest.mark.parametrize(
    "agent_name",
    [
        "weather-agent-cycle",
        "healthcare-agent-cycle",
        "finance-agent-cycle",
        "travel-agent-cycle",
        "support-ticket-agent-cycle",
    ],
)
def test_wait_for_telemetry_discovers_exact_attempt_operations_for_every_agent(
    monkeypatch,
    agent_name,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    operations = ("a" * 32, "b" * 32)
    captured = []
    timespans = []

    class Table:
        rows = [
            [operations[0], ["resp_A1b2C3d4E5f6"]],
            [operations[1], ["resp_F6e5D4c3B2a1"]],
        ]

    class Result:
        status = "success"
        tables = [Table()]

    runtime = _runtime()
    monotonic = [0.0]
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._monotonic = lambda: monotonic[0]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )

    def query(_client, query, **kwargs):
        captured.append(query)
        timespans.append(kwargs["timespan"])
        return Result()

    runtime._query_resource = query  # type: ignore[method-assign]
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=(
            "resp_A1b2C3d4E5f6",
            "resp_F6e5D4c3B2a1",
        ),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=2,
        allow_window_correlation=False,
    )

    assert runtime.wait_for_telemetry(
        agent_name=agent_name,
        foundry_version="cycle-version",
        invocation=invocation,
    ) == operations
    query_text = captured[0]
    assert len(captured) == 2
    assert "union traces, dependencies, requests" in query_text
    assert 'customDimensions["gen_ai.operation.name"]' in query_text
    assert 'customDimensions["gen_ai.response.id"]' in query_text
    assert 'customDimensions["x-ms-client-request-id"]' in query_text
    assert 'operation_name == "invoke_agent"' in query_text
    assert agent_name not in query_text
    assert "cycle-version" not in query_text
    assert "matched_references=make_set(matched_reference)" in query_text
    assert "timestamp >= datetime(2026-08-28T10:00:00+00:00)" in query_text
    assert "timestamp <= datetime(2026-08-28T10:00:01+00:00)" in query_text
    assert "order by first_seen asc" in query_text
    assert timespans == [
        (
            datetime.fromisoformat(invocation.started_at),
            datetime.fromisoformat(invocation.completed_at),
        )
    ] * 2


def test_validation_telemetry_identity_is_derived_per_exact_operation(
    monkeypatch,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    operations = ("a" * 32, "b" * 32)
    captured = []

    rows = [
        [
            [
                operations[0],
                "resp_A1b2C3d4E5f6",
                ["finance-agent"],
                ["opaque-version"],
            ]
        ],
        [
            [
                operations[0],
                "resp_A1b2C3d4E5f6",
                ["finance-agent"],
                ["opaque-version"],
            ],
            [
                operations[1],
                "resp_F6e5D4c3B2a1",
                ["finance-agent"],
                ["opaque-version"],
            ],
        ],
        [
            [
                operations[0],
                "resp_A1b2C3d4E5f6",
                ["finance-agent"],
                ["opaque-version"],
            ],
            [
                operations[1],
                "resp_F6e5D4c3B2a1",
                ["finance-agent"],
                ["opaque-version"],
            ],
        ],
    ]

    runtime = _runtime()
    monotonic = [0.0]
    sleeps = []
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._monotonic = lambda: monotonic[0]

    def sleep(seconds):
        sleeps.append(seconds)
        monotonic[0] += seconds

    runtime._sleep = sleep

    def query(_client, query, **_kwargs):
        captured.append(query)
        current = rows.pop(0)
        table = type("Table", (), {"rows": current})()
        return type("Result", (), {"status": "success", "tables": [table]})()

    runtime._query_resource = query  # type: ignore[method-assign]
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=(
            "resp_A1b2C3d4E5f6",
            "resp_F6e5D4c3B2a1",
        ),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=2,
        allow_window_correlation=False,
    )
    assert runtime.telemetry_identity_passes(
        agent_name="finance-agent",
        foundry_version="opaque-version",
        operation_ids=operations,
        invocation=invocation,
    ) == (True, True)
    assert sleeps == [15, 15]
    assert len(captured) == 3
    assert "make_set(observed_agent)" in captured[0]
    assert "make_set(agent_version)" in captured[0]
    assert "matched_reference in" in captured[0]


@pytest.mark.parametrize(
    ("agent_names", "agent_versions"),
    [
        (["other-agent"], ["opaque-version"]),
        (["finance-agent", "other-agent"], ["opaque-version"]),
        (["finance-agent"], ["opaque-version", "other-version"]),
    ],
)
def test_validation_telemetry_identity_fails_closed_on_wrong_or_multiple_values(
    monkeypatch,
    agent_names,
    agent_versions,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    operation_id = "a" * 32
    table = type(
        "Table",
        (),
        {
            "rows": [
                [
                    operation_id,
                    "resp_A1b2C3d4E5f6",
                    agent_names,
                    agent_versions,
                ]
            ]
        },
    )()
    result = type("Result", (), {"status": "success", "tables": [table]})()
    runtime = _runtime()
    sleeps = []
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = lambda *_args, **_kwargs: result  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=("resp_A1b2C3d4E5f6",),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=1,
        allow_window_correlation=False,
    )

    assert runtime.telemetry_identity_passes(
        agent_name="finance-agent",
        foundry_version="opaque-version",
        operation_ids=(operation_id,),
        invocation=invocation,
    ) == (False,)
    assert sleeps == []


def test_validation_telemetry_identity_ignores_other_response_in_same_operation(
    monkeypatch,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    operation_id = "a" * 32
    reference = "resp_A1b2C3d4E5f6"
    table = type(
        "Table",
        (),
        {
            "rows": [
                [
                    operation_id,
                    reference,
                    ["finance-agent"],
                    ["opaque-version"],
                ],
                [
                    operation_id,
                    "resp_F6e5D4c3B2a1",
                    ["other-agent"],
                    ["other-version"],
                ],
            ]
        },
    )()
    result = type("Result", (), {"status": "success", "tables": [table]})()
    runtime = _runtime()
    monotonic = [0.0]
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = lambda *_args, **_kwargs: result  # type: ignore[method-assign]
    runtime._monotonic = lambda: monotonic[0]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0,
        monotonic[0] + seconds,
    )
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=(reference,),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=1,
        allow_window_correlation=False,
    )

    assert runtime.telemetry_identity_passes(
        agent_name="finance-agent",
        foundry_version="opaque-version",
        operation_ids=(operation_id,),
        invocation=invocation,
    ) == (True,)


def test_validation_telemetry_identity_rejects_conflict_during_stabilization(
    monkeypatch,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    operation_id = "a" * 32
    rows = [
        [
            [
                operation_id,
                "resp_A1b2C3d4E5f6",
                ["finance-agent"],
                ["opaque-version"],
            ]
        ],
        [
            [
                operation_id,
                "resp_A1b2C3d4E5f6",
                ["finance-agent", "other-agent"],
                ["opaque-version"],
            ]
        ],
    ]
    runtime = _runtime()
    monotonic = [0.0]
    sleeps = []
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._monotonic = lambda: monotonic[0]

    def sleep(seconds):
        sleeps.append(seconds)
        monotonic[0] += seconds

    runtime._sleep = sleep

    def query(*_args, **_kwargs):
        table = type("Table", (), {"rows": rows.pop(0)})()
        return type("Result", (), {"status": "success", "tables": [table]})()

    runtime._query_resource = query  # type: ignore[method-assign]
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=("resp_A1b2C3d4E5f6",),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=1,
        allow_window_correlation=False,
    )

    assert runtime.telemetry_identity_passes(
        agent_name="finance-agent",
        foundry_version="opaque-version",
        operation_ids=(operation_id,),
        invocation=invocation,
    ) == (False,)
    assert sleeps == [15]


def test_validation_telemetry_identity_missing_value_waits_to_deadline(
    monkeypatch,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    monkeypatch.setattr(
        "agent_insights_quality.live.TRACE_ASSERTION_DEADLINE_SECONDS",
        30,
    )
    operation_id = "a" * 32
    table = type(
        "Table",
        (),
        {"rows": [[operation_id, "resp_A1b2C3d4E5f6", [], []]]},
    )()
    result = type("Result", (), {"status": "success", "tables": [table]})()
    runtime = _runtime()
    monotonic = [0.0]
    sleeps = []
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = lambda *_args, **_kwargs: result  # type: ignore[method-assign]
    runtime._monotonic = lambda: monotonic[0]

    def sleep(seconds):
        sleeps.append(seconds)
        monotonic[0] += seconds

    runtime._sleep = sleep
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=("resp_A1b2C3d4E5f6",),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=1,
        allow_window_correlation=False,
    )

    assert runtime.telemetry_identity_passes(
        agent_name="finance-agent",
        foundry_version="opaque-version",
        operation_ids=(operation_id,),
        invocation=invocation,
    ) == (False,)
    assert sleeps == [15, 15]


def test_validation_telemetry_identity_does_not_interpolate_values(monkeypatch) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    operation_id = "a" * 32
    agent_name = 'finance-agent"\n| take 1'
    foundry_version = "opaque\\version"
    table = type(
        "Table",
        (),
        {
            "rows": [
                [
                    operation_id,
                    "resp_A1b2C3d4E5f6",
                    [agent_name],
                    [foundry_version],
                ]
            ]
        },
    )()
    result = type("Result", (), {"status": "success", "tables": [table]})()
    captured = []
    runtime = _runtime()
    monotonic = [0.0]
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda _client, query, **_kwargs: captured.append(query) or result
    )
    runtime._monotonic = lambda: monotonic[0]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0,
        monotonic[0] + seconds,
    )
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=("resp_A1b2C3d4E5f6",),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=1,
        allow_window_correlation=False,
    )

    assert runtime.telemetry_identity_passes(
        agent_name=agent_name,
        foundry_version=foundry_version,
        operation_ids=(operation_id,),
        invocation=invocation,
    ) == (True,)
    assert agent_name not in captured[0]
    assert foundry_version not in captured[0]


def test_wait_for_telemetry_waits_for_exact_count_before_stabilizing(
    monkeypatch,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    first = "a" * 32
    second = "b" * 32
    first_row = [first, ["resp_A1b2C3d4E5f6"]]
    second_row = [second, ["resp_F6e5D4c3B2a1"]]
    rows = [
        [first_row],
        [first_row],
        [first_row],
        [first_row, second_row],
        [first_row, second_row],
    ]

    class Result:
        status = "success"

        def __init__(self, current_rows):
            self.tables = [type("Table", (), {"rows": current_rows})()]

    runtime = _runtime()
    monotonic = [0.0]
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: Result(rows.pop(0))
    )
    runtime._monotonic = lambda: monotonic[0]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=(
            "resp_A1b2C3d4E5f6",
            "resp_F6e5D4c3B2a1",
        ),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=2,
        allow_window_correlation=False,
    )

    assert runtime.wait_for_telemetry(
        agent_name="finance-agent",
        foundry_version="opaque-version",
        invocation=invocation,
    ) == (first, second)
    assert monotonic[0] == 60


def test_wait_for_telemetry_timeout_records_safe_reference_counts(
    monkeypatch,
) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    monotonic = [0.0]

    class Table:
        rows = []

    class Result:
        status = "success"
        tables = [Table()]

    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = lambda *_args, **_kwargs: Result()  # type: ignore[method-assign]
    runtime._monotonic = lambda: monotonic[0]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=(
            "resp_A1b2C3d4E5f6",
            "resp_F6e5D4c3B2a1",
        ),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=2,
        allow_window_correlation=False,
    )

    with pytest.raises(TelemetryCorrelationError) as caught:
        runtime.wait_for_telemetry(
            agent_name="weather-agent",
            foundry_version="1",
            invocation=invocation,
        )

    assert caught.value.request_accepted is True
    assert caught.value.matched_reference_count == 0
    assert caught.value.expected_reference_count == 2
    assert caught.value.missing_reference_count == 2
    assert monotonic[0] == 15 * 60


def test_wait_for_telemetry_exact_set_change_resets_stability(monkeypatch) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    first = "a" * 32
    second = "b" * 32
    reference = "resp_A1b2C3d4E5f6"
    rows = [
        [[first, [reference]]],
        [[second, [reference]]],
        [[second, [reference]]],
    ]
    monotonic = [0.0]

    class Result:
        status = "success"

        def __init__(self, current_rows):
            self.tables = [type("Table", (), {"rows": current_rows})()]

    runtime = _runtime()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: Result(rows.pop(0))
    )
    runtime._monotonic = lambda: monotonic[0]
    runtime._sleep = lambda seconds: monotonic.__setitem__(
        0, monotonic[0] + seconds
    )
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=("resp_A1b2C3d4E5f6",),
        started_at="2026-08-28T10:00:00+00:00",
        completed_at="2026-08-28T10:00:01+00:00",
        request_count=1,
        allow_window_correlation=False,
    )

    assert runtime.wait_for_telemetry(
        agent_name="weather-agent",
        foundry_version="opaque-version",
        invocation=invocation,
    ) == (second,)
    assert monotonic[0] == 30


def test_trace_contract_waits_for_child_span_hydration() -> None:
    operation_id = "a" * 32

    class Partial:
        rows = [[operation_id, ["invoke_agent"], 1, 1]]

    class Complete:
        rows = [[operation_id, ["invoke_agent", "chat"], 1, 2]]

    assert (
        _trace_contract_ready(
            [Partial()],
            (operation_id,),
            (("invoke_agent", "chat"),),
        )
        is False
    )
    assert (
        _trace_contract_ready(
            [Complete()],
            (operation_id,),
            (("invoke_agent", "chat"),),
        )
        is True
    )


def test_trace_contract_correlates_required_operations_per_request() -> None:
    first = "a" * 32
    second = "b" * 32

    class Complete:
        rows = [
            [first, ["invoke_agent", "execute_tool", "chat"], 1, 3],
            [second, ["invoke_agent", "execute_tool"], 1, 2],
        ]

    required = (
        ("invoke_agent", "execute_tool", "chat"),
        ("invoke_agent", "execute_tool"),
    )
    assert _trace_contract_ready([Complete()], (first, second), required) is True
    assert (
        _trace_contract_ready(
            [Complete()],
            (first, second),
            (required[0], required[0]),
        )
        is False
    )
    assert (
        _trace_contract_ready(
            [Complete()],
            (second, first),
            required,
        )
        is False
    )


def test_runtime_caches_tokens_and_serializes_telemetry_queries() -> None:
    calls = 0

    def token_provider(_scope):
        nonlocal calls
        calls += 1
        return "synthetic-token"

    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=token_provider,
    )
    assert runtime._token_provider("scope") == "synthetic-token"
    assert runtime._token_provider("scope") == "synthetic-token"
    assert calls == 1

    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    class Client:
        def query_resource(self, *_args, **_kwargs):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return object()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(
                runtime._query_resource,
                Client(),
                "query",
                timespan=(0, 1),
            )
            for _ in range(5)
        ]
        for future in futures:
            future.result()
    assert maximum_active == 1


def test_telemetry_query_retries_transient_sdk_failures(monkeypatch) -> None:
    class SyntheticHttpError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    runtime = _runtime()
    attempts = 0
    sleeps = []

    class Client:
        def query_resource(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 4:
                raise SyntheticHttpError(503)
            return "complete"

    monkeypatch.setattr(
        "agent_insights_quality.live._TELEMETRY_HTTP_ERRORS",
        (SyntheticHttpError,),
    )
    monkeypatch.setattr(
        "agent_insights_quality.live._TELEMETRY_TRANSIENT_ERRORS",
        (SyntheticHttpError,),
    )
    runtime._sleep = sleeps.append
    assert (
        runtime._query_resource(Client(), "query", timespan=(0, 1))
        == "complete"
    )
    assert attempts == 4
    assert sleeps == [1, 2, 4]


def test_telemetry_query_does_not_retry_nontransient_http_failures(
    monkeypatch,
) -> None:
    class SyntheticHttpError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    runtime = _runtime()
    attempts = 0

    class Client:
        def query_resource(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise SyntheticHttpError(400)

    monkeypatch.setattr(
        "agent_insights_quality.live._TELEMETRY_HTTP_ERRORS",
        (SyntheticHttpError,),
    )
    monkeypatch.setattr(
        "agent_insights_quality.live._TELEMETRY_TRANSIENT_ERRORS",
        (SyntheticHttpError,),
    )
    with pytest.raises(TelemetryQueryError) as caught:
        runtime._query_resource(Client(), "query", timespan=(0, 1))
    assert attempts == 1
    assert isinstance(caught.value.__cause__, SyntheticHttpError)


def test_agent_insights_checkpoint_is_persisted_before_polling() -> None:
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        utcnow=lambda: now,
    )
    checkpoint = InsightRunCheckpoint("private-run-id", {})
    persisted = []
    runtime._start_insights_once = (  # type: ignore[method-assign]
        lambda **_kwargs: checkpoint
    )
    runtime._operation_time_bounds = (  # type: ignore[method-assign]
        lambda **_kwargs: (
            now - timedelta(minutes=1),
            now - timedelta(seconds=30),
        )
    )
    result = runtime.start_insights_run(
        agent_name="weather-agent",
        monitor_id="monitor-weather",
        foundry_version="1",
        operation_ids=("a" * 32,),
        lookback_hours=0.1,
        start_margin_seconds=30,
        persist=persisted.append,
    )
    assert result == checkpoint
    assert persisted == [checkpoint]


def test_integral_lookback_uses_service_compatible_integer() -> None:
    runtime = _runtime()
    captured = {}
    runtime._insight_revisions = lambda _monitor: {}  # type: ignore[method-assign]

    def request(method, url, body=None, **_kwargs):
        captured.update({"method": method, "url": url, "body": body})
        return {"id": "private-run-id"}

    runtime._json_request = request  # type: ignore[method-assign]
    checkpoint = runtime._start_insights_once(
        monitor_id="private-monitor",
        lookback_hours=1.0,
    )
    assert checkpoint.run_id == "private-run-id"
    assert captured["method"] == "POST"
    assert captured["body"]["lookback_hours"] == 1
    assert isinstance(captured["body"]["lookback_hours"], int)


def test_agent_insights_rejects_operations_outside_short_window() -> None:
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        utcnow=lambda: now,
    )
    runtime._operation_time_bounds = (  # type: ignore[method-assign]
        lambda **_kwargs: (
            now - timedelta(minutes=7),
            now - timedelta(minutes=6, seconds=30),
        )
    )
    runtime._start_insights_once = (  # type: ignore[method-assign]
        lambda **_kwargs: pytest.fail("expired operations must not start Insights")
    )
    with pytest.raises(InsightWindowExpiredError, match="expired"):
        runtime.start_insights_run(
            agent_name="weather-agent",
            monitor_id="monitor-weather",
            foundry_version="1",
            operation_ids=("a" * 32,),
            lookback_hours=0.1,
            start_margin_seconds=30,
            persist=lambda _checkpoint: None,
        )


def test_agent_insights_rechecks_age_after_revision_fetch() -> None:
    now = [datetime(2026, 8, 27, 18, 0, tzinfo=UTC)]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        utcnow=lambda: now[0],
    )
    runtime._operation_time_bounds = (  # type: ignore[method-assign]
        lambda **_kwargs: (
            now[0] - timedelta(minutes=5),
            now[0] - timedelta(minutes=4, seconds=30),
        )
    )

    def delayed_revisions(_monitor_id: str) -> dict:
        now[0] += timedelta(seconds=31)
        return {}

    runtime._insight_revisions = delayed_revisions  # type: ignore[method-assign]
    runtime._json_request = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: pytest.fail(
            "expired operation window must not send an Insights POST"
        )
    )

    with pytest.raises(InsightWindowExpiredError, match="expired"):
        runtime.start_insights_run(
            agent_name="weather-agent",
            monitor_id="monitor-weather",
            foundry_version="1",
            operation_ids=("a" * 32,),
            lookback_hours=0.1,
            start_margin_seconds=30,
            persist=lambda _checkpoint: pytest.fail(
                "expired Insight start must not persist a checkpoint"
            ),
        )


def test_agent_insights_auth_retry_stops_at_start_deadline(monkeypatch) -> None:
    now = [datetime(2026, 8, 27, 18, 0, tzinfo=UTC)]
    tokens = iter(["expired-token", "fresh-token"])
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _scope: next(tokens),
        utcnow=lambda: now[0],
    )
    runtime._operation_time_bounds = (  # type: ignore[method-assign]
        lambda **_kwargs: (
            now[0] - timedelta(minutes=5, seconds=29),
            now[0] - timedelta(minutes=5),
        )
    )
    runtime._insight_revisions = lambda _monitor: {}  # type: ignore[method-assign]
    attempts = 0
    timeouts = []

    def open_request(*_args, timeout):
        nonlocal attempts
        attempts += 1
        timeouts.append(timeout)
        now[0] += timedelta(seconds=1)
        raise urllib.error.HTTPError(
            "https://example.invalid",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"{}"),
        )

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )

    with pytest.raises(InsightWindowExpiredError, match="expired"):
        runtime.start_insights_run(
            agent_name="weather-agent",
            monitor_id="monitor-weather",
            foundry_version="1",
            operation_ids=("a" * 32,),
            lookback_hours=0.1,
            start_margin_seconds=30,
            persist=lambda _checkpoint: pytest.fail(
                "expired retry must not persist a checkpoint"
            ),
        )

    assert attempts == 1
    assert timeouts == [1.0]


def test_agent_insights_rejects_window_anchor_race() -> None:
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    earliest = now - timedelta(minutes=5)
    latest = now - timedelta(minutes=4)
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        utcnow=lambda: now,
    )

    runtime._operation_time_bounds = (  # type: ignore[method-assign]
        lambda **_kwargs: (earliest, latest)
    )
    runtime._wait_insights_run = (  # type: ignore[method-assign]
        lambda *_args: {
            "status": "succeeded",
            "window_start": (earliest + timedelta(seconds=1)).isoformat(),
            "window_end": (now + timedelta(minutes=1)).isoformat(),
        }
    )
    runtime._list_insights = lambda _monitor_id: []  # type: ignore[method-assign]
    with pytest.raises(InsightWindowExpiredError, match="excluded"):
        runtime.finish_insights_run(
            agent_name="weather-agent",
            monitor_id="monitor-weather",
            foundry_version="1",
            operation_ids=("a" * 32,),
            checkpoint=InsightRunCheckpoint("private-run-id", {}),
        )


def test_rejected_insight_drain_skips_scoring_window_validation() -> None:
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    earliest = now - timedelta(minutes=5)
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        utcnow=lambda: now,
    )
    runtime._wait_insights_run = (  # type: ignore[method-assign]
        lambda *_args: {
            "status": "succeeded",
            "window_start": (earliest + timedelta(seconds=1)).isoformat(),
            "window_end": (now + timedelta(minutes=1)).isoformat(),
        }
    )
    runtime._list_insights = lambda _monitor_id: []  # type: ignore[method-assign]
    runtime._operation_time_bounds = (  # type: ignore[method-assign]
        lambda **_kwargs: pytest.fail("drain must not validate scoring window")
    )

    result = runtime.finish_insights_run(
        agent_name="weather-agent",
        monitor_id="monitor-weather",
        foundry_version="1",
        operation_ids=("a" * 32,),
        checkpoint=InsightRunCheckpoint("private-run-id", {}),
        validate_window=False,
    )

    assert result.status == "succeeded"


def test_clean_window_waits_for_private_ledger_horizon(monkeypatch) -> None:
    query_module = types.ModuleType("azure.monitor.query")
    query_module.LogsQueryStatus = type("LogsQueryStatus", (), {"SUCCESS": "success"})
    monitor_module = types.ModuleType("azure.monitor")
    azure_module = types.ModuleType("azure")
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_module)
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    now = [datetime(2026, 8, 27, 18, 0, tzinfo=UTC)]
    monotonic = [0.0]
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _: "synthetic-token",
        utcnow=lambda: now[0],
        monotonic=lambda: monotonic[0],
    )
    ready_at = now[0] + timedelta(seconds=120)
    queries = []

    class Ledger:
        @staticmethod
        def clean_after(*_args, **_kwargs):
            return ready_at

    class Table:
        rows = [[None, 0]]

    class Result:
        status = "success"
        tables = [Table()]

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)
        monotonic[0] += seconds

    def query(_client, query_text, *, timespan):
        queries.append((query_text, timespan))
        return Result()

    runtime._traffic_ledger = Ledger()
    runtime._logs_client = lambda: object()  # type: ignore[method-assign]
    runtime._query_resource = query  # type: ignore[method-assign]
    runtime._sleep = sleep
    runtime.wait_for_clean_window(
        "weather-agent",
        0.1,
        poll_seconds=30,
        ingestion_margin_seconds=30,
        max_wait_seconds=180,
    )
    assert monotonic[0] == 120
    assert all("ago(390s)" in query_text for query_text, _ in queries)


def test_json_get_retries_transient_http_failures(monkeypatch) -> None:
    runtime = _runtime()
    attempts = 0
    sleeps = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"value":"ok"}'

    def open_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid",
                503,
                "Unavailable",
                {},
                io.BytesIO(b"{}"),
            )
        return Response()

    runtime._sleep = sleeps.append
    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    value = runtime._json_request("GET", "https://example.invalid")
    assert value["value"] == "ok"
    assert attempts == 2
    assert sleeps == [1]


def test_json_post_retries_foundry_failed_dependency(monkeypatch) -> None:
    runtime = _runtime()
    attempts = 0
    request_references = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"value":"ok"}'

    def open_request(request, **_kwargs):
        nonlocal attempts
        attempts += 1
        request_references.append(request.headers["X-ms-client-request-id"])
        if attempts == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid",
                424,
                "Failed Dependency",
                {},
                io.BytesIO(b"{}"),
            )
        return Response()

    runtime._sleep = lambda _seconds: None
    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    value = runtime._json_request(
        "POST",
        "https://example.invalid",
        retry_statuses={424},
    )
    assert value["value"] == "ok"
    assert attempts == 2
    assert len(set(request_references)) == 2
    assert value["_request_reference"] == request_references[-1]


@pytest.mark.parametrize(
    ("status", "code", "request_accepted"),
    [
        (429, "too_many_requests", False),
        (503, "service_unavailable", None),
    ],
)
def test_json_post_classifies_sanitized_remote_failures(
    monkeypatch,
    status,
    code,
    request_accepted,
) -> None:
    runtime = _runtime()
    progress = []
    runtime.report_progress = progress.append  # type: ignore[method-assign]

    def open_request(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://example.invalid",
            status,
            "Synthetic failure",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "error": {
                            "code": code,
                            "message": "private diagnostic must not escape",
                        }
                    }
                ).encode()
            ),
        )

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    with pytest.raises(RemoteOperationError) as caught:
        runtime._json_request("POST", "https://example.invalid")

    assert caught.value.status == status
    assert caught.value.code == code
    assert caught.value.request_accepted is request_accepted
    assert "private diagnostic" not in str(caught.value)
    assert progress == [f"remote POST rejected: status={status}; code={code}"]


@pytest.mark.parametrize("payload", [b"[]", b"\xff"])
def test_json_success_with_invalid_payload_is_accepted_contract_failure(
    monkeypatch,
    payload,
) -> None:
    runtime = _runtime()

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return payload

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    with pytest.raises(RemoteOperationError) as caught:
        runtime._json_request("POST", "https://example.invalid")

    assert caught.value.status == 200
    assert caught.value.code in {"invalid_json", "invalid_json_shape"}
    assert caught.value.request_accepted is True


def test_prompt_retries_exact_first_agent_route_propagation_rejection() -> None:
    runtime = _runtime()
    mark_started = []
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {
            "mark_started": staticmethod(
                lambda *args, **kwargs: mark_started.append((args, kwargs))
            )
        },
    )()
    calls = []
    sleeps = []
    progress = []

    def json_request(method, url, body, **kwargs):
        calls.append((method, url, dict(body), kwargs))
        if len(calls) == 1:
            raise RemoteOperationError(
                "Remote operation failed with HTTP 404 (NotFound)",
                code="NotFound",
                status=404,
                request_accepted=False,
            )
        return {"id": "response-accepted", "output": []}

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    runtime.report_progress = progress.append  # type: ignore[method-assign]
    result = runtime._invoke_prompt(
        "weather-agent",
        "1",
        {
            "body": {"input": "Synthetic first request."},
            "expected_status": 200,
            "semantic_assertions": {},
            "activation_gate": False,
        },
        0,
        None,
        include_seed_metadata=False,
        validation_intent_reference="sha256:" + ("a" * 64),
    )

    assert result[0] == ["response-accepted"]
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://example.invalid/openai/v1/responses"
    assert calls[0][2] == {
        "input": "Synthetic first request.",
        "store": True,
        "agent_reference": {
            "type": "agent_reference",
            "name": "weather-agent",
            "version": "1",
        },
        "metadata": {
            "validation_intent_reference": "sha256:" + ("a" * 64),
        },
    }
    assert sleeps == [1]
    assert len(mark_started) == 1
    assert progress == [
        "weather-agent/1: exact Prompt Agent route is not yet available; "
        "retrying first request in 1s (2/5)"
    ]


def test_prompt_first_agent_route_retry_is_bounded() -> None:
    runtime = _runtime()
    runtime._monotonic = lambda: 0
    mark_started = []
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {
            "mark_started": staticmethod(
                lambda *args, **kwargs: mark_started.append((args, kwargs))
            )
        },
    )()
    calls = 0
    sleeps = []
    error = RemoteOperationError(
        "Remote operation failed with HTTP 404 (NotFound)",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def json_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {"input": "Synthetic first request."},
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            0,
            None,
            include_seed_metadata=False,
        )

    assert caught.value is error
    assert calls == 5
    assert sleeps == [1, 2, 4, 8]
    assert len(mark_started) == 1


def test_prompt_first_agent_route_retry_observes_deadline() -> None:
    runtime = _runtime()
    now = [0]
    runtime._monotonic = lambda: now[0]
    calls = 0
    sleeps = []
    error = RemoteOperationError(
        "Remote operation failed with HTTP 404 (NotFound)",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def json_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        now[0] = 15
        raise error

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {"input": "Synthetic first request."},
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            0,
            None,
            include_seed_metadata=False,
        )

    assert caught.value is error
    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "error",
    [
        RemoteOperationError(
            "Synthetic accepted rejection",
            code="NotFound",
            status=404,
            request_accepted=True,
        ),
        RemoteOperationError(
            "Synthetic ambiguous rejection",
            code="NotFound",
            status=404,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic unrelated code",
            code="not_found",
            status=404,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic unrelated status",
            code="NotFound",
            status=400,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic no response",
            code="remote_no_response",
            status=None,
            request_accepted=None,
        ),
    ],
)
def test_prompt_first_request_does_not_retry_unrelated_failures(error) -> None:
    runtime = _runtime()
    calls = 0
    sleeps = []

    def json_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {"input": "Synthetic first request."},
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            0,
            None,
            include_seed_metadata=False,
        )

    assert caught.value is error
    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "body",
    [
        {"input": "Synthetic request.", "store": True},
        {
            "input": "Synthetic request.",
            "store": True,
            "agent_reference": {
                "type": "model",
                "name": "weather-agent",
                "version": "1",
            },
        },
        {
            "input": "Synthetic request.",
            "store": True,
            "agent_reference": {
                "type": "agent_reference",
                "name": "other-agent",
                "version": "1",
            },
        },
        {
            "input": "Synthetic request.",
            "store": True,
            "agent_reference": {
                "type": "agent_reference",
                "name": "weather-agent",
                "version": "2",
            },
        },
        {
            "input": "Synthetic request.",
            "store": True,
            "previous_response_id": "previous-response",
            "agent_reference": {
                "type": "agent_reference",
                "name": "weather-agent",
                "version": "1",
            },
        },
    ],
)
def test_prompt_agent_route_retry_rejects_non_exact_agent_requests(body) -> None:
    assert not _prompt_agent_route_propagation_pending(
        RemoteOperationError(
            "Remote operation failed with HTTP 404 (NotFound)",
            code="NotFound",
            status=404,
            request_accepted=False,
        ),
        body=body,
        agent_name="weather-agent",
        foundry_version="1",
    )


@pytest.mark.parametrize("code", ["NotFound", "previous_response_not_found"])
def test_prompt_retries_exact_previous_response_propagation_rejection(code) -> None:
    runtime = _runtime()
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {"mark_started": staticmethod(lambda *_args, **_kwargs: None)},
    )()
    calls = []
    sleeps = []

    def json_request(method, url, body, **kwargs):
        calls.append((method, url, body, kwargs))
        if len(calls) == 1:
            raise RemoteOperationError(
                f"Remote operation failed with HTTP 404 ({code})",
                code=code,
                status=404,
                request_accepted=False,
            )
        return {"id": "response-accepted", "output": []}

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    result = runtime._invoke_prompt(
        "weather-agent",
        "1",
        {
            "body": {"input": "Synthetic chained request."},
            "expected_status": 200,
            "semantic_assertions": {},
            "activation_gate": False,
        },
        0,
        "previous-response",
        include_seed_metadata=False,
        validation_intent_reference="sha256:" + ("a" * 64),
    )

    assert result[0] == ["response-accepted"]
    assert len(calls) == 2
    assert all(call[0] == "POST" for call in calls)
    assert all(
        call[1] == "https://example.invalid/openai/v1/responses" for call in calls
    )
    assert all(call[2]["previous_response_id"] == "previous-response" for call in calls)
    assert sleeps == [1]


@pytest.mark.parametrize(
    ("code", "rejection_count", "expected_sleeps"),
    [
        ("NotFound", 3, [1, 2, 4]),
        ("previous_response_not_found", 4, [1, 2, 4, 8]),
    ],
)
def test_prompt_chained_response_retry_succeeds_at_extended_delays(
    code,
    rejection_count,
    expected_sleeps,
) -> None:
    runtime = _runtime()
    mark_started = []
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {
            "mark_started": staticmethod(
                lambda *args, **kwargs: mark_started.append((args, kwargs))
            )
        },
    )()
    calls = []
    sleeps = []

    def json_request(method, url, body, **kwargs):
        calls.append((method, url, body, kwargs))
        if len(calls) <= rejection_count:
            raise RemoteOperationError(
                f"Remote operation failed with HTTP 404 ({code})",
                code=code,
                status=404,
                request_accepted=False,
            )
        return {"id": "response-accepted", "output": []}

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    result = runtime._invoke_prompt(
        "weather-agent",
        "1",
        {
            "body": {
                "input": "Synthetic chained request.",
                "metadata": {"logical_attempt": "fixed"},
            },
            "expected_status": 200,
            "semantic_assertions": {},
            "activation_gate": False,
        },
        0,
        "previous-response",
        include_seed_metadata=False,
    )

    assert result[0] == ["response-accepted"]
    assert len(calls) == rejection_count + 1
    assert all(call == calls[0] for call in calls)
    assert all(call[2] is calls[0][2] for call in calls)
    assert calls[0][2]["metadata"] == {"logical_attempt": "fixed"}
    assert calls[0][2]["previous_response_id"] == "previous-response"
    assert sleeps == expected_sleeps
    assert len(mark_started) == 1


@pytest.mark.parametrize(
    ("error", "previous_response_id"),
    [
        (
            RemoteOperationError(
                "Remote operation failed with HTTP 404 (NotFound)",
                code="NotFound",
                status=404,
                request_accepted=None,
            ),
            "previous-response",
        ),
        (
            RemoteOperationError(
                "Remote operation failed with HTTP 404 (NotFound)",
                code="NotFound",
                status=404,
                request_accepted=True,
            ),
            "previous-response",
        ),
        (
            RemoteOperationError(
                "Remote operation failed with HTTP 404 (notfound)",
                code="notfound",
                status=404,
                request_accepted=False,
            ),
            "previous-response",
        ),
        (
            RemoteOperationError(
                "Remote operation failed with HTTP 400 "
                "(previous_response_not_found)",
                code="previous_response_not_found",
                status=400,
                request_accepted=False,
            ),
            "previous-response",
        ),
        (
            RemoteOperationError(
                "Remote operation failed with HTTP 404 (invalid_request)",
                code="invalid_request",
                status=404,
                request_accepted=False,
            ),
            "previous-response",
        ),
        (
            RemoteOperationError(
                "Remote operation failed before a response was received",
                code="remote_no_response",
                status=None,
                request_accepted=None,
            ),
            "previous-response",
        ),
        (
            RemoteOperationError(
                "Remote operation failed with HTTP 408 (request_timeout)",
                code="request_timeout",
                status=408,
                request_accepted=None,
            ),
            "previous-response",
        ),
        (
            RemoteOperationError(
                "Remote operation failed with HTTP 503 (service_unavailable)",
                code="service_unavailable",
                status=503,
                request_accepted=None,
            ),
            "previous-response",
        ),
    ],
)
def test_prompt_does_not_retry_ambiguous_or_unrelated_failures(
    error,
    previous_response_id,
) -> None:
    runtime = _runtime()
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {"mark_started": staticmethod(lambda *_args, **_kwargs: None)},
    )()
    calls = 0
    sleeps = []

    def json_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {"input": "Synthetic chained request."},
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            0,
            previous_response_id,
            include_seed_metadata=False,
        )

    assert caught.value is error
    assert calls == 1
    assert sleeps == []


def test_prompt_chained_response_retry_is_bounded() -> None:
    runtime = _runtime()
    mark_started = []
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {
            "mark_started": staticmethod(
                lambda *args, **kwargs: mark_started.append((args, kwargs))
            )
        },
    )()
    calls = 0
    sleeps = []
    error = RemoteOperationError(
        "Remote operation failed with HTTP 404 (NotFound)",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def json_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {"input": "Synthetic chained request."},
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            0,
            "previous-response",
            include_seed_metadata=False,
        )

    assert caught.value is error
    assert calls == 5
    assert sleeps == [1, 2, 4, 8]
    assert len(mark_started) == 1


def test_prompt_does_not_retry_post_response_contract_failure() -> None:
    runtime = _runtime()
    runtime._traffic_ledger = type(
        "Ledger",
        (),
        {"mark_started": staticmethod(lambda *_args, **_kwargs: None)},
    )()
    calls = 0
    sleeps = []

    def json_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"output": []}

    runtime._json_request = json_request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_prompt(
            "weather-agent",
            "1",
            {
                "body": {"input": "Synthetic chained request."},
                "expected_status": 200,
                "semantic_assertions": {},
                "activation_gate": False,
            },
            0,
            "previous-response",
            include_seed_metadata=False,
        )

    assert caught.value.code == "prompt_response_identity_missing"
    assert caught.value.request_accepted is True
    assert calls == 1
    assert sleeps == []


def test_json_patch_retries_no_response_with_explicit_policy(monkeypatch) -> None:
    runtime = _runtime()
    request_references = []
    sleeps = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"value":"ok"}'

    def open_request(request, **_kwargs):
        request_references.append(request.headers["X-ms-client-request-id"])
        if len(request_references) == 1:
            raise TimeoutError("synthetic no-response timeout")
        return Response()

    runtime._sleep = sleeps.append
    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    value = runtime._json_request(
        "PATCH",
        "https://example.invalid",
        retry_statuses={408, 503},
        retry_no_response=True,
    )
    assert value["value"] == "ok"
    assert len(set(request_references)) == 2
    assert value["_request_reference"] == request_references[-1]
    assert sleeps == [1]


def test_json_patch_bounds_no_response_retries(monkeypatch) -> None:
    runtime = _runtime()
    attempts = 0
    sleeps = []

    def open_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("synthetic no-response timeout")

    runtime._sleep = sleeps.append
    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    with pytest.raises(
        ContractError,
        match="Remote operation failed before a response was received",
    ):
        runtime._json_request(
            "PATCH",
            "https://example.invalid",
            retry_statuses={408, 503},
            retry_no_response=True,
        )
    assert attempts == 3
    assert sleeps == [1, 2]


def test_json_post_requires_explicit_no_response_retry(monkeypatch) -> None:
    runtime = _runtime()
    attempts = 0

    def open_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("synthetic no-response timeout")

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    with pytest.raises(
        RemoteOperationError,
        match="Remote operation failed before a response was received",
    ) as caught:
        runtime._json_request(
            "POST",
            "https://example.invalid",
            retry_statuses={408, 503},
        )
    assert attempts == 1
    assert caught.value.code == "remote_no_response"
    assert caught.value.status is None
    assert caught.value.request_accepted is None


def test_hosted_invocation_persists_endpoint_response_identity(
    monkeypatch,
) -> None:
    runtime = _runtime()
    timeout_seconds = None
    request_reference = None
    response_reference = "resp_A1b2C3d4E5f6"

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return json.dumps(
                {
                    "id": response_reference,
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "complete"}
                            ],
                        }
                    ],
                }
            ).encode()

    def open_request(request, *, timeout):
        nonlocal request_reference, timeout_seconds
        request_reference = request.headers["X-ms-client-request-id"]
        timeout_seconds = timeout
        return Response()

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    (
        references,
        usable,
        assertion_count,
        assertions_passed,
        direct_terminal_response_count,
        function_call_count,
        assertion_results,
        activation_gate,
    ) = runtime._invoke_hosted(
            "finance-agent",
            "session-id",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )
    assert references == [response_reference]
    assert request_reference != response_reference
    assert usable is True
    assert assertion_count == 0
    assert assertions_passed == 0
    assert direct_terminal_response_count == 0
    assert function_call_count == 0
    assert assertion_results == ()
    assert activation_gate is False
    assert timeout_seconds == 600


@pytest.mark.parametrize("response_id", [None, "", "has whitespace", 17])
def test_hosted_invocation_rejects_invalid_endpoint_response_identity(
    monkeypatch,
    response_id,
) -> None:
    runtime = _runtime()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return json.dumps({"id": response_id, "output": []}).encode()

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    with pytest.raises(ContractError, match="identity is missing or invalid"):
        runtime._invoke_hosted(
            "finance-agent",
            "session_A1b2C3d4",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )


def test_hosted_cleanup_failure_preserves_completed_responses() -> None:
    runtime = _runtime()
    progress = []
    runtime._activate_hosted_version = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: None
    )
    runtime._create_hosted_session = (  # type: ignore[method-assign]
        lambda *_args: "session-id"
    )
    runtime._invoke_hosted = (  # type: ignore[method-assign]
        lambda *_args: (["successful-attempt"], True, 0, 0, 0, 0, (), False)
    )
    runtime._delete_hosted_session = (  # type: ignore[method-assign]
        lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic cleanup failure"))
    )
    runtime.report_progress = progress.append  # type: ignore[method-assign]

    results = runtime._invoke_group(
        "finance-agent",
        "hosted",
        "19",
        [
            {
                "_index": 0,
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            }
        ],
        1,
    )

    assert results == [
        (0, ["successful-attempt"], True, 0, 0, 0, 0, (), False)
    ]
    assert progress == [
        "finance-agent/19: session cleanup failed after endpoint completion; "
        "preserving completed evidence"
    ]


def test_hosted_cleanup_failure_preserves_primary_invocation_error() -> None:
    runtime = _runtime()
    progress = []
    runtime._activate_hosted_version = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: None
    )
    runtime._create_hosted_session = (  # type: ignore[method-assign]
        lambda *_args: "session-id"
    )
    runtime._invoke_hosted = (  # type: ignore[method-assign]
        lambda *_args: (_ for _ in ()).throw(ValueError("primary invocation failure"))
    )
    runtime._delete_hosted_session = (  # type: ignore[method-assign]
        lambda *_args: (_ for _ in ()).throw(RuntimeError("secondary cleanup failure"))
    )
    runtime.report_progress = progress.append  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="primary invocation failure"):
        runtime._invoke_group(
            "finance-agent",
            "hosted",
            "19",
            [
                {
                    "_index": 0,
                    "body": {"input": "synthetic request"},
                    "expected_status": 200,
                }
            ],
            1,
        )

    assert progress == ["finance-agent/19: session cleanup also failed"]


def test_mixed_group_failures_preserve_ambiguous_traffic_horizon(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    traffic = tmp_path / "traffic.json"
    traffic.write_text(
        json.dumps(
            [
                {
                    "id": "known-http",
                    "request": {"body": {"input": "synthetic one"}},
                },
                {
                    "id": "ambiguous",
                    "request": {"body": {"input": "synthetic two"}},
                },
            ]
        ),
        encoding="utf-8",
    )

    class Ledger:
        completed = 0

        @staticmethod
        def mark_started(*_args, **_kwargs):
            return None

        def mark_completed(self, *_args, **_kwargs):
            self.completed += 1

    ledger = Ledger()
    runtime._traffic_ledger = ledger

    def invoke_group(_agent, _type, _version, fixtures, _seed):
        if fixtures[0]["id"] == "known-http":
            raise ContractError("Remote operation failed with HTTP 424")
        raise ContractError("Remote operation failed before a response was received")

    runtime._invoke_group = invoke_group  # type: ignore[method-assign]
    with pytest.raises(ContractError, match="before a response"):
        runtime.invoke_version(
            agent_name="finance-agent",
            agent_type="hosted",
            foundry_version="1",
            traffic_path=traffic,
            seed=1,
        )
    assert ledger.completed == 0


def test_json_request_refreshes_an_expired_credential_once(monkeypatch) -> None:
    tokens = iter(["expired-token", "fresh-token"])
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _scope: next(tokens),
    )
    authorization = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"value":"ok"}'

    def open_request(request, **_kwargs):
        authorization.append(request.headers["Authorization"])
        if len(authorization) == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid",
                401,
                "Unauthorized",
                {},
                io.BytesIO(b"{}"),
            )
        return Response()

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    value = runtime._json_request("POST", "https://example.invalid")
    assert value["value"] == "ok"
    assert authorization == ["Bearer expired-token", "Bearer fresh-token"]


def test_json_request_rechecks_start_deadline_after_auth_refresh(
    monkeypatch,
) -> None:
    initial = datetime(2026, 8, 29, 12, tzinfo=UTC)
    now = [initial]
    attempts = 0
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _scope: "synthetic-token",
        utcnow=lambda: now[0],
    )

    def open_request(_request, **_kwargs):
        nonlocal attempts
        attempts += 1
        now[0] += timedelta(seconds=2)
        raise urllib.error.HTTPError(
            "https://example.invalid",
            401,
            "synthetic expired credential",
            {},
            io.BytesIO(b"{}"),
        )

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    with pytest.raises(InsightWindowExpiredError, match="expired"):
        runtime._json_request(
            "POST",
            "https://example.invalid",
            request_deadline=initial + timedelta(seconds=1),
        )

    assert attempts == 1


def test_json_request_reserves_auth_refresh_after_transient_retries(
    monkeypatch,
) -> None:
    tokens = iter(["expired-token", "fresh-token"])
    runtime = LiveRuntime(
        _runtime()._profile,
        token_provider=lambda _scope: next(tokens),
        sleep=lambda _seconds: None,
    )
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"value":"ok"}'

    def open_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        status = 503 if attempts <= 18 else 401 if attempts == 19 else 200
        if status != 200:
            raise urllib.error.HTTPError(
                "https://example.invalid",
                status,
                "Synthetic failure",
                {},
                io.BytesIO(b"{}"),
            )
        return Response()

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    value = runtime._json_request("GET", "https://example.invalid")
    assert value["value"] == "ok"
    assert attempts == 20


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_routing_uses_one_fixed_ratio_rule(agent_name) -> None:
    runtime = _runtime()
    captured = {}

    def request(method, url, body=None, **kwargs):
        captured.update(
            {
                "method": method,
                "url": url,
                "body": body,
                "content_type": kwargs["content_type"],
                "retry_statuses": kwargs["retry_statuses"],
                "retry_no_response": kwargs["retry_no_response"],
                "retry_unauthorized": kwargs["retry_unauthorized"],
            }
        )
        return body

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._activate_hosted_version(agent_name, "7")
    rules = captured["body"]["agent_endpoint"]["version_selector"][
        "version_selection_rules"
    ]
    assert captured["method"] == "PATCH"
    assert captured["url"].endswith(f"/agents/{agent_name}")
    assert captured["content_type"] == "application/merge-patch+json"
    assert captured["retry_statuses"] == set()
    assert captured["retry_no_response"] is False
    assert captured["retry_unauthorized"] is False
    assert rules == [
        {
            "agent_version": "7",
            "traffic_percentage": 100,
            "type": "FixedRatio",
        }
    ]


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_routing_retries_exact_activation_then_caches(agent_name) -> None:
    runtime = _runtime()
    now = [0]
    attempts = []
    sleeps = []
    progress = []
    error = RemoteOperationError(
        "Synthetic route activation propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(method, url, body=None, **kwargs):
        attempts.append((method, url, body, kwargs))
        if len(attempts) < 3:
            raise error
        return body

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    runtime._monotonic = lambda: now[0]
    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleep
    runtime.report_progress = progress.append  # type: ignore[method-assign]
    runtime._activate_hosted_version(agent_name, "7")
    confirmed_at = runtime._hosted_routes[agent_name][1]
    runtime._activate_hosted_version(agent_name, "7")
    assert runtime._hosted_routes[agent_name][1] == confirmed_at
    now[0] = 100
    runtime._activate_hosted_version(agent_name, "7", refresh_route=True)

    assert len(attempts) == 4
    assert all(item[0] == "PATCH" for item in attempts)
    assert all(item[1].endswith(f"/agents/{agent_name}") for item in attempts)
    assert all(item[2] is attempts[0][2] for item in attempts[:3])
    assert attempts[3][2] == attempts[0][2]
    assert all(item[3]["retry_statuses"] == set() for item in attempts)
    assert all(item[3]["retry_no_response"] is False for item in attempts)
    assert all(item[3]["retry_unauthorized"] is False for item in attempts)
    assert sleeps == [5, 10]
    assert runtime._hosted_routes[agent_name] == ("7", 100)
    assert progress == [
        f"{agent_name}/7: exact Hosted route activation is not yet available; "
        "retrying the same selector in 5s (2/5)",
        f"{agent_name}/7: exact Hosted route activation is not yet available; "
        "retrying the same selector in 10s (3/5)",
    ]


def test_hosted_routing_exhausts_bounded_activation_retries() -> None:
    runtime = _runtime()
    now = [0]
    attempts = 0
    sleeps = []
    error = RemoteOperationError(
        "Synthetic route activation propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise error

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    runtime._monotonic = lambda: now[0]
    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleep

    with pytest.raises(RemoteOperationError) as caught:
        runtime._activate_hosted_version("finance-agent", "1")

    assert caught.value is error
    assert attempts == 5
    assert sleeps == [5, 10, 20, 30]
    assert "finance-agent" not in runtime._hosted_routes


@pytest.mark.parametrize(
    "error",
    [
        RemoteOperationError(
            "Synthetic accepted rejection",
            code="NotFound",
            status=404,
            request_accepted=True,
        ),
        RemoteOperationError(
            "Synthetic ambiguous rejection",
            code="NotFound",
            status=404,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic unrelated code",
            code="not_found",
            status=404,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic unrelated status",
            code="NotFound",
            status=400,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic no response",
            code="remote_no_response",
            status=None,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic timeout",
            code="NotFound",
            status=408,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic service failure",
            code="NotFound",
            status=503,
            request_accepted=None,
        ),
    ],
)
def test_hosted_routing_does_not_retry_ambiguous_or_unrelated_failures(
    error,
) -> None:
    runtime = _runtime()
    attempts = 0
    sleeps = []

    def request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append

    with pytest.raises(RemoteOperationError) as caught:
        runtime._activate_hosted_version("travel-agent", "1")

    assert caught.value is error
    assert attempts == 1
    assert sleeps == []
    assert "travel-agent" not in runtime._hosted_routes


def test_hosted_routing_caches_only_confirmed_exact_version() -> None:
    runtime = _runtime()
    attempts = 0

    def request(_method, _url, body=None, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {
                "agent_endpoint": {
                    "version_selector": {
                        "version_selection_rules": [
                            {
                                "agent_version": "2",
                                "traffic_percentage": 100,
                                "type": "FixedRatio",
                            }
                        ]
                    }
                }
            }
        return body

    runtime._json_request = request  # type: ignore[method-assign]

    with pytest.raises(
        ContractError,
        match="did not confirm exact-version routing",
    ):
        runtime._activate_hosted_version("support-ticket-agent", "1")

    assert "support-ticket-agent" not in runtime._hosted_routes

    runtime._activate_hosted_version("support-ticket-agent", "1")
    runtime._activate_hosted_version("support-ticket-agent", "1")

    assert attempts == 2
    assert runtime._hosted_routes["support-ticket-agent"][0] == "1"


def test_hosted_session_retries_exact_post_activation_not_found() -> None:
    runtime = _runtime()
    runtime._monotonic = lambda: 0
    sleeps = []
    session_bodies = []

    def request(method, _url, body=None, **_kwargs):
        if method == "PATCH":
            return body
        session_bodies.append(body)
        if len(session_bodies) == 1:
            raise RemoteOperationError(
                "Synthetic route propagation",
                code="NotFound",
                status=404,
                request_accepted=False,
            )
        return {
            "agent_session_id": "session-synthetic",
            "version_indicator": {
                "type": "version_ref",
                "agent_version": "1",
            },
        }

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    runtime._activate_hosted_version("finance-agent", "1")
    session_id = runtime._create_hosted_session(
        "finance-agent",
        "1",
        validation_intent_reference="sha256:" + ("a" * 64),
    )

    assert session_id == "session-synthetic"
    assert len(session_bodies) == 2
    assert session_bodies[0] == session_bodies[1]
    assert sleeps == [1]


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_each_hosted_attempt_refreshes_session_retry_proof_after_release(
    agent_name,
) -> None:
    runtime = _runtime()
    now = [0]
    runtime._monotonic = lambda: now[0]
    patch_calls = []
    session_calls = []
    released_sessions = []
    sleeps = []

    def request(method, url, body=None, **kwargs):
        if method == "PATCH":
            patch_calls.append((body, kwargs))
            return body
        if method == "DELETE":
            released_sessions.append(url.rsplit("/", 1)[-1])
            return {}
        session_calls.append((body, kwargs))
        if len(session_calls) == 2:
            raise RemoteOperationError(
                "Synthetic route propagation",
                code="NotFound",
                status=404,
                request_accepted=False,
            )
        session_number = 1 if len(session_calls) == 1 else 2
        return {
            "agent_session_id": f"session-{session_number}",
            "version_indicator": {
                "type": "version_ref",
                "agent_version": "1",
            },
        }

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleep

    runtime._activate_hosted_version(agent_name, "1")
    now[0] = 203
    runtime._activate_hosted_version(agent_name, "1", refresh_route=True)
    first_session = runtime._create_hosted_session(
        agent_name,
        "1",
        validation_intent_reference="sha256:" + ("a" * 64),
    )
    runtime._delete_hosted_session(agent_name, first_session)

    now[0] = 927
    runtime._activate_hosted_version(agent_name, "1", refresh_route=True)
    second_session = runtime._create_hosted_session(
        agent_name,
        "1",
        validation_intent_reference="sha256:" + ("b" * 64),
    )
    runtime._delete_hosted_session(agent_name, second_session)

    assert len(patch_calls) == 3
    assert runtime._hosted_routes[agent_name] == ("1", 927)
    assert [first_session, second_session] == ["session-1", "session-2"]
    assert released_sessions == ["session-1", "session-2"]
    assert len(session_calls) == 3
    assert session_calls[1][0] is session_calls[2][0]
    assert session_calls[0][0] != session_calls[1][0]
    assert all(call[1]["retry_statuses"] == set() for call in session_calls)
    assert all(call[1]["retry_no_response"] is False for call in session_calls)
    assert all(call[1]["retry_unauthorized"] is False for call in session_calls)
    assert sleeps == [1]
    assert not runtime._hosted_session_bindings


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_response_retries_each_fixed_request_in_same_session(
    agent_name,
) -> None:
    runtime = _runtime()
    _disable_traffic_ledger(runtime)
    now = [0]
    runtime._monotonic = lambda: now[0]
    sleeps = []
    progress = []
    session_calls = 0
    session_releases = 0
    response_calls = []
    response_attempts = {}

    def request(method, url, body=None, **kwargs):
        nonlocal session_calls, session_releases
        if method == "PATCH":
            return body
        if method == "DELETE":
            session_releases += 1
            return {}
        if url.endswith("/endpoint/sessions"):
            session_calls += 1
            return {
                "agent_session_id": "session-synthetic",
                "version_indicator": {
                    "type": "version_ref",
                    "agent_version": "1",
                },
            }
        response_calls.append((method, url, body, kwargs))
        request_text = body["input"]
        response_attempts[request_text] = response_attempts.get(request_text, 0) + 1
        if response_attempts[request_text] == 1:
            raise RemoteOperationError(
                "Synthetic response route propagation",
                code="NotFound",
                status=404,
                request_accepted=False,
            )
        if request_text == "synthetic first request":
            now[0] = 100
        return {
            "id": (
                "response-first"
                if request_text == "synthetic first request"
                else "response-second"
            ),
            "output": [],
        }

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleep
    runtime.report_progress = progress.append  # type: ignore[method-assign]

    result = runtime._invoke_group(
        agent_name,
        "hosted",
        "1",
        [
            {
                "_index": 0,
                "body": {"input": "synthetic first request"},
                "expected_status": 200,
            },
            {
                "_index": 1,
                "body": {"input": "synthetic second request"},
                "expected_status": 200,
            }
        ],
        1,
    )

    assert [item[1] for item in result] == [
        ["response-first"],
        ["response-second"],
    ]
    assert session_calls == 1
    assert session_releases == 1
    assert len(response_calls) == 4
    assert all(call[0] == "POST" for call in response_calls)
    assert all(
        call[1].endswith(
            f"/agents/{agent_name}/endpoint/protocols/openai/responses"
        )
        for call in response_calls
    )
    assert all(call[3]["retry_statuses"] == set() for call in response_calls)
    assert all(call[3]["retry_no_response"] is False for call in response_calls)
    assert all(call[3]["retry_unauthorized"] is False for call in response_calls)
    assert response_calls[0][2] == response_calls[1][2] == {
        "input": "synthetic first request",
        "agent_session_id": "session-synthetic",
        "store": False,
    }
    assert response_calls[2][2] == response_calls[3][2] == {
        "input": "synthetic second request",
        "agent_session_id": "session-synthetic",
        "store": False,
    }
    assert (
        response_calls[0][3]["correlation_id"]
        == response_calls[1][3]["correlation_id"]
    )
    assert (
        response_calls[2][3]["correlation_id"]
        == response_calls[3][3]["correlation_id"]
    )
    assert (
        response_calls[0][3]["correlation_id"]
        != response_calls[2][3]["correlation_id"]
    )
    assert sleeps == [1, 1]
    assert progress == [
        f"{agent_name}/1: Hosted response route is not yet available; retrying "
        "the same session request in 1s (2/5)",
        f"{agent_name}/1: Hosted response route is not yet available; retrying "
        "the same session request in 1s (2/5)",
    ]
    assert (agent_name, "session-synthetic") not in runtime._hosted_session_bindings


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_response_does_not_refresh_unauthorized_request(
    monkeypatch,
    agent_name,
) -> None:
    runtime = _runtime()
    _disable_traffic_ledger(runtime)
    runtime._hosted_routes[agent_name] = ("1", 0)
    runtime._hosted_session_bindings[(agent_name, "session-synthetic")] = (
        "1",
        0,
    )
    request_references = []

    def open_request(request, **_kwargs):
        request_references.append(request.headers["X-ms-client-request-id"])
        raise urllib.error.HTTPError(
            "https://example.invalid",
            401,
            "Synthetic unauthorized",
            {},
            io.BytesIO(b'{"error":{"code":"Unauthorized"}}'),
        )

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )

    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_hosted(
            agent_name,
            "session-synthetic",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )

    assert caught.value.status == 401
    assert caught.value.code == "Unauthorized"
    assert caught.value.request_accepted is False
    assert len(request_references) == 1


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_response_exhausts_bounded_route_propagation_retries(
    agent_name,
) -> None:
    runtime = _runtime()
    _disable_traffic_ledger(runtime)
    now = [0]
    runtime._monotonic = lambda: now[0]
    sleeps = []
    session_calls = 0
    session_releases = 0
    response_calls = 0
    error = RemoteOperationError(
        "Synthetic response route propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(method, url, body=None, **_kwargs):
        nonlocal response_calls, session_calls, session_releases
        if method == "PATCH":
            return body
        if method == "DELETE":
            session_releases += 1
            return {}
        if url.endswith("/endpoint/sessions"):
            session_calls += 1
            return {
                "agent_session_id": "session-synthetic",
                "version_indicator": {
                    "type": "version_ref",
                    "agent_version": "1",
                },
            }
        response_calls += 1
        raise error

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleep

    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_group(
            agent_name,
            "hosted",
            "1",
            [
                {
                    "_index": 0,
                    "body": {"input": "synthetic request"},
                    "expected_status": 200,
                }
            ],
            1,
        )

    assert caught.value is error
    assert session_calls == 1
    assert session_releases == 1
    assert response_calls == 5
    assert sleeps == [1, 2, 4, 8]
    assert now[0] == 15


@pytest.mark.parametrize(
    "error",
    [
        RemoteOperationError(
            "Synthetic accepted rejection",
            code="NotFound",
            status=404,
            request_accepted=True,
        ),
        RemoteOperationError(
            "Synthetic ambiguous rejection",
            code="NotFound",
            status=404,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic no response",
            code="remote_no_response",
            status=None,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic timeout",
            code="NotFound",
            status=408,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic service failure",
            code="NotFound",
            status=503,
            request_accepted=False,
        ),
    ],
)
@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_response_does_not_retry_accepted_unknown_or_transient_failures(
    error,
    agent_name,
) -> None:
    runtime = _runtime()
    _disable_traffic_ledger(runtime)
    runtime._hosted_routes[agent_name] = ("1", 0)
    runtime._hosted_session_bindings[(agent_name, "session-synthetic")] = (
        "1",
        0,
    )
    runtime._monotonic = lambda: 0
    calls = 0

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]

    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_hosted(
            agent_name,
            "session-synthetic",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )

    assert caught.value is error
    assert calls == 1


@pytest.mark.parametrize(
    "error",
    [
        RemoteOperationError(
            "Synthetic unrelated code",
            code="not_found",
            status=404,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic unrelated status",
            code="NotFound",
            status=400,
            request_accepted=False,
        ),
    ],
)
@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_response_does_not_retry_unrelated_rejections(
    error,
    agent_name,
) -> None:
    runtime = _runtime()
    _disable_traffic_ledger(runtime)
    runtime._hosted_routes[agent_name] = ("1", 0)
    runtime._hosted_session_bindings[(agent_name, "session-synthetic")] = (
        "1",
        0,
    )
    runtime._monotonic = lambda: 0
    calls = 0

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]

    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_hosted(
            agent_name,
            "session-synthetic",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )

    assert caught.value is error
    assert calls == 1


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
@pytest.mark.parametrize("binding_state", ["missing", "wrong-route"])
def test_hosted_response_requires_exact_session_binding(
    binding_state,
    agent_name,
) -> None:
    runtime = _runtime()
    _disable_traffic_ledger(runtime)
    runtime._monotonic = lambda: 0
    if binding_state != "missing":
        runtime._hosted_session_bindings[
            (agent_name, "session-synthetic")
        ] = ("1", 0)
    runtime._hosted_routes[agent_name] = ("2", 0)
    calls = 0
    sleeps = []
    error = RemoteOperationError(
        "Synthetic response route propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append

    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_hosted(
            agent_name,
            "session-synthetic",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )

    assert caught.value is error
    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_response_stops_retry_when_exact_route_changes_during_delay(
    agent_name,
) -> None:
    runtime = _runtime()
    _disable_traffic_ledger(runtime)
    runtime._hosted_routes[agent_name] = ("1", 0)
    runtime._hosted_session_bindings[(agent_name, "session-synthetic")] = (
        "1",
        0,
    )
    runtime._monotonic = lambda: 0
    calls = 0
    sleeps = []
    error = RemoteOperationError(
        "Synthetic response route propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    def sleep(delay):
        sleeps.append(delay)
        runtime._hosted_routes[agent_name] = ("2", 0)

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleep

    with pytest.raises(RemoteOperationError) as caught:
        runtime._invoke_hosted(
            agent_name,
            "session-synthetic",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )

    assert caught.value is error
    assert calls == 1
    assert sleeps == [1]


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_session_exhausts_bounded_route_propagation_retries(
    agent_name,
) -> None:
    runtime = _runtime()
    runtime._monotonic = lambda: 0
    sleeps = []
    session_calls = 0
    error = RemoteOperationError(
        "Synthetic route propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(method, _url, body=None, **_kwargs):
        nonlocal session_calls
        if method == "PATCH":
            return body
        session_calls += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    runtime._activate_hosted_version(agent_name, "1")

    with pytest.raises(RemoteOperationError) as caught:
        runtime._create_hosted_session(agent_name, "1")

    assert caught.value is error
    assert session_calls == 5
    assert sleeps == [1, 2, 4, 8]
    assert not runtime._hosted_session_bindings


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
@pytest.mark.parametrize(
    "error",
    [
        RemoteOperationError(
            "Synthetic unrelated rejection",
            code="not_found",
            status=404,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic unrelated rejection",
            code="NotFound",
            status=400,
            request_accepted=False,
        ),
        RemoteOperationError(
            "Synthetic accepted rejection",
            code="NotFound",
            status=404,
            request_accepted=True,
        ),
        RemoteOperationError(
            "Synthetic ambiguous rejection",
            code="NotFound",
            status=404,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic no response",
            code="remote_no_response",
            status=None,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic timeout",
            code="NotFound",
            status=408,
            request_accepted=None,
        ),
        RemoteOperationError(
            "Synthetic service failure",
            code="NotFound",
            status=503,
            request_accepted=None,
        ),
    ],
)
def test_hosted_session_does_not_retry_unrelated_or_ambiguous_failures(
    error,
    agent_name,
) -> None:
    runtime = _runtime()
    runtime._monotonic = lambda: 0
    calls = 0
    sleeps = []

    def request(method, _url, body=None, **_kwargs):
        nonlocal calls
        if method == "PATCH":
            return body
        calls += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._sleep = sleeps.append
    runtime._activate_hosted_version(agent_name, "1")

    with pytest.raises(RemoteOperationError) as caught:
        runtime._create_hosted_session(agent_name, "1")

    assert caught.value is error
    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_session_does_not_refresh_unauthorized_request(
    monkeypatch,
    agent_name,
) -> None:
    runtime = _runtime()
    runtime._hosted_routes[agent_name] = ("1", 0)
    runtime._monotonic = lambda: 0
    attempts = 0

    def open_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            "https://example.invalid",
            401,
            "Synthetic unauthorized",
            {},
            io.BytesIO(b'{"error":{"code":"Unauthorized"}}'),
        )

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )

    with pytest.raises(RemoteOperationError) as caught:
        runtime._create_hosted_session(agent_name, "1")

    assert caught.value.status == 401
    assert caught.value.request_accepted is False
    assert attempts == 1


@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_session_does_not_retry_without_recent_exact_activation(
    agent_name,
) -> None:
    runtime = _runtime()
    calls = 0
    error = RemoteOperationError(
        "Synthetic route propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]

    with pytest.raises(RemoteOperationError) as caught:
        runtime._create_hosted_session(agent_name, "1")

    assert caught.value is error
    assert calls == 1


@pytest.mark.parametrize(
    ("routed_version", "session_start"),
    [("2", 0), ("1", 16)],
)
@pytest.mark.parametrize(
    "agent_name",
    ["finance-agent", "travel-agent", "support-ticket-agent"],
)
def test_hosted_session_does_not_retry_wrong_or_expired_route(
    agent_name,
    routed_version,
    session_start,
) -> None:
    runtime = _runtime()
    now = [0]
    runtime._monotonic = lambda: now[0]
    calls = 0
    error = RemoteOperationError(
        "Synthetic route propagation",
        code="NotFound",
        status=404,
        request_accepted=False,
    )

    def request(method, _url, body=None, **_kwargs):
        nonlocal calls
        if method == "PATCH":
            return body
        calls += 1
        raise error

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._activate_hosted_version(agent_name, routed_version)
    now[0] = session_start

    with pytest.raises(RemoteOperationError) as caught:
        runtime._create_hosted_session(agent_name, "1")

    assert caught.value is error
    assert calls == 1


@pytest.mark.parametrize("hosted", [False, True])
def test_unrelated_post_does_not_retry_agent_route_not_found(
    monkeypatch,
    hosted,
) -> None:
    runtime = _runtime()
    attempts = 0

    def open_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            "https://example.invalid",
            404,
            "Synthetic route propagation",
            {},
            io.BytesIO(b'{"error":{"code":"NotFound"}}'),
        )

    monkeypatch.setattr(
        "agent_insights_quality.live.urllib.request.urlopen",
        open_request,
    )
    with pytest.raises(RemoteOperationError):
        runtime._json_request(
            "POST",
            "https://example.invalid",
            hosted=hosted,
        )

    assert attempts == 1
