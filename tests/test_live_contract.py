from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_insights_quality.live import (
    LiveRuntime,
    _complete_operation_ids,
    _normalize_fixture,
    _semantic_assertion_result,
    _trace_behavior_summary,
    _trace_contract_ready,
    _usable_response,
)
from agent_insights_quality.profiles import RuntimeProfile


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
    count, passed = _semantic_assertion_result(
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
    }
    assert "private" not in json.dumps(summary)


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


def test_repeated_tool_fixtures_are_argument_aware() -> None:
    value = _normalize_fixture(
        {
            "id": "health-baseline",
            "request": {"body": {"input": "synthetic lookup"}},
            "tool_fixtures": [
                {
                    "tool": "lookup_slots",
                    "arguments": {"date": "2030-01-01"},
                    "returns": {"slots": ["a"]},
                },
                {
                    "tool": "lookup_slots",
                    "arguments": {"date": "2030-01-02"},
                    "returns": {"slots": ["b"]},
                },
            ],
        }
    )
    assert len(value["tool_outputs"]["lookup_slots"]) == 2


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
            ("invoke_agent", "chat"),
        )
        is False
    )
    assert (
        _trace_contract_ready(
            [Complete()],
            (operation_id,),
            ("invoke_agent", "chat"),
        )
        is True
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
    with pytest.raises(SyntheticHttpError):
        runtime._query_resource(Client(), "query", timespan=(0, 1))
    assert attempts == 1


def test_failed_agent_insights_run_retries_without_new_traffic() -> None:
    runtime = _runtime()
    statuses = iter(["failed", "failed", "succeeded"])
    sleeps = []

    class Result:
        def __init__(self, status):
            self.status = status

    runtime._run_insights_once = (  # type: ignore[method-assign]
        lambda **_kwargs: Result(next(statuses))
    )
    runtime._sleep = sleeps.append
    result = runtime.run_insights(
        agent_name="weather-agent",
        monitor_id="monitor-weather",
        foundry_version="1",
        operation_ids=("a" * 32,),
        lookback_hours=3,
    )
    assert result.status == "succeeded"
    assert sleeps == [30, 30]


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


def test_hosted_invocation_correlates_with_successful_request_reference(
    monkeypatch,
) -> None:
    runtime = _runtime()
    timeout_seconds = None
    request_reference = None

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
                    "id": "response-id-not-present-in-telemetry",
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
    references, usable, assertion_count, assertions_passed = (
        runtime._invoke_hosted(
            "finance-agent",
            "session-id",
            {
                "body": {"input": "synthetic request"},
                "expected_status": 200,
            },
            1,
        )
    )
    assert references == [request_reference]
    assert usable is True
    assert assertion_count == 0
    assert assertions_passed == 0
    assert timeout_seconds == 600


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


def test_hosted_routing_uses_one_fixed_ratio_rule() -> None:
    runtime = _runtime()
    captured = {}

    def request(method, url, body=None, **kwargs):
        captured.update(
            {
                "method": method,
                "url": url,
                "body": body,
                "content_type": kwargs["content_type"],
            }
        )
        return body

    runtime._json_request = request  # type: ignore[method-assign]
    runtime._activate_hosted_version("travel-agent", "7")
    rules = captured["body"]["agent_endpoint"]["version_selector"][
        "version_selection_rules"
    ]
    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/agents/travel-agent")
    assert captured["content_type"] == "application/merge-patch+json"
    assert rules == [
        {
            "agent_version": "7",
            "traffic_percentage": 100,
            "type": "FixedRatio",
        }
    ]
