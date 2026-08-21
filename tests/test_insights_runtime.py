from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from agent_insights_quality.insights.client import AgentInsightsClient, HttpResponse
from agent_insights_quality.insights.telemetry import (
    TelemetryExpectation,
    correlate_complete_traces,
    wait_for_correlated_traces,
)
from agent_insights_quality.runtime.errors import RuntimeFailure


class Credential:
    class Token:
        token = "test-secret-token-value"

    def get_token(self, scope):
        assert scope == "https://ai.azure.com/.default"
        return self.Token()


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, json_body, timeout):
        self.requests.append((method, url, headers, json_body, timeout))
        return self.responses.pop(0)


def response(payload, status=200):
    return HttpResponse(status, {}, json.dumps(payload).encode())


def test_client_sends_real_bearer_header_without_exposing_it_in_errors() -> None:
    transport = FakeTransport([response({"data": []})])
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=transport,
    )
    client.list_monitors()
    assert transport.requests[0][2]["Authorization"] == "Bearer test-secret-token-value"
    assert "test-secret" not in repr(client)


def test_client_paginates_runs_and_fetches_every_insight_detail() -> None:
    base = "https://project.example.invalid"
    transport = FakeTransport(
        [
            response({"data": [{"id": "r1"}], "nextLink": base + "/next?api-version=x"}),
            response({"data": [{"id": "r2"}]}),
            response({"data": [{"id": "i1"}], "nextLink": base + "/insights-next?api-version=x"}),
            response({"data": [{"id": "i2"}]}),
            response({"id": "i1", "details": {"title": "one"}}),
            response({"id": "i2", "details": {"title": "two"}}),
        ]
    )
    client = AgentInsightsClient(base, Credential(), transport=transport)
    assert [item["id"] for item in client.list_runs("monitor")] == ["r1", "r2"]
    details = client.list_insights("monitor", "run")
    assert [item["id"] for item in details] == ["i1", "i2"]
    detail_urls = [request[1] for request in transport.requests[-2:]]
    assert all("include_details=true" in url for url in detail_urls)


def test_client_maps_failed_run_and_enforces_five_insight_gate() -> None:
    failed = FakeTransport([response({"id": "run", "status": "failed", "error": {"code": "bad"}})])
    client = AgentInsightsClient("https://project.example.invalid", Credential(), transport=failed)
    with pytest.raises(RuntimeFailure, match="terminal state failed") as failure:
        client.wait_run("monitor", "run", timeout_seconds=1)
    assert failure.value.details == {"service_error_code": "bad"}

    responses = [
        response({"id": "run", "status": "succeeded"}),
        response({"data": [{"id": f"i{index}"} for index in range(6)]}),
        *[response({"id": f"i{index}", "details": {}}) for index in range(6)],
    ]
    too_many = FakeTransport(responses)
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=too_many,
    )
    with pytest.raises(RuntimeFailure, match="more than five"):
        client.collect_run("monitor", "run")
    assert len(too_many.requests) == 2


def test_client_rejects_path_and_pagination_injection() -> None:
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=FakeTransport([response({"data": [], "nextLink": "https://evil.invalid/page"})]),
    )
    with pytest.raises(RuntimeFailure, match="changed endpoint"):
        client.list_runs("monitor")
    with pytest.raises(RuntimeFailure, match="invalid"):
        client.get_run("../monitor", "run")


def test_monitor_reuse_reset_and_cleanup_require_exact_ownership_and_expiry() -> None:
    wrong_owner = FakeTransport(
        [response({"data": [{"id": "m1", "agent_name": "agent", "metadata": {"owner_reference": "other"}}]})]
    )
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=wrong_owner,
    )
    with pytest.raises(RuntimeFailure, match="does not match"):
        client.get_or_create_monitor(
            agent_name="agent",
            model_deployment_name="terra",
            owner_reference="owner",
        )

    cleanup = FakeTransport(
        [
            response(
                {
                    "data": [
                        {
                            "id": "expired",
                            "metadata": {
                                "purpose": "agent-insights-quality",
                                "owner_reference": "owner",
                                "expires_on": "2026-08-20",
                            },
                        },
                        {
                            "id": "future",
                            "metadata": {
                                "purpose": "agent-insights-quality",
                                "owner_reference": "owner",
                                "expires_on": "2026-08-22",
                            },
                        },
                    ]
                }
            ),
            HttpResponse(204, {}, b""),
        ]
    )
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=cleanup,
    )
    assert client.cleanup_owned_monitors(
        "owner",
        now=date(2026, 8, 21),
        dry_run=False,
    ) == ["expired"]


def telemetry_rows(start: datetime):
    trace = "a" * 32
    common = {
        "timestamp": start + timedelta(seconds=1),
        "operation_id": trace,
        "project_name": "aiq-20260821",
        "agent_name": "agent",
        "agent_version": "v1",
        "invocation_id": "invoke-1",
        "response_id": "response-1",
        "hosted_response_id": "",
        "session_id": "session-1",
        "span_agent_name": "agent",
        "span_agent_version": "v1",
        "span_model": "terra-deployment",
    }
    return [
        common | {"span_id": "root", "parent_id": "", "span_name": "invoke_agent"},
        common | {"span_id": "child", "parent_id": "root", "span_name": "chat"},
    ]


def test_correlates_ids_to_w3c_operation_and_requires_complete_parent_chain() -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    expectation = TelemetryExpectation("invoke-1", "response-1", "session-1", "terra-deployment")
    result = correlate_complete_traces(
        telemetry_rows(start),
        [expectation],
        project="aiq-20260821",
        agent="agent",
        version="v1",
        start=start,
        end=start + timedelta(minutes=1),
    )
    assert result and result[0].operation_id == "a" * 32
    incomplete = telemetry_rows(start)
    incomplete[1]["parent_id"] = "missing"
    assert correlate_complete_traces(
        incomplete,
        [expectation],
        project="aiq-20260821",
        agent="agent",
        version="v1",
        start=start,
        end=start + timedelta(minutes=1),
    ) is None
    wrong_model = telemetry_rows(start)
    wrong_model[1]["span_model"] = "other-model"
    assert correlate_complete_traces(
        wrong_model,
        [expectation],
        project="aiq-20260821",
        agent="agent",
        version="v1",
        start=start,
        end=start + timedelta(minutes=1),
    ) is None


def test_correlation_prefers_invocation_ids_when_sessions_are_shared() -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    first = telemetry_rows(start)
    second = [
        row
        | {
            "operation_id": "b" * 32,
            "invocation_id": "invoke-2",
            "response_id": "response-2",
        }
        for row in telemetry_rows(start)
    ]
    result = correlate_complete_traces(
        first + second,
        [
            TelemetryExpectation("invoke-1", "response-1", "session-1", "terra-deployment"),
            TelemetryExpectation("invoke-2", "response-2", "session-1", "terra-deployment"),
        ],
        project="aiq-20260821",
        agent="agent",
        version="v1",
        start=start,
        end=start + timedelta(minutes=1),
    )
    assert result and [item.operation_id for item in result] == ["a" * 32, "b" * 32]


def test_ingestion_polling_is_bounded_and_fails_closed() -> None:
    class Query:
        def query(self, *_args):
            return []

    ticks = iter([0.0, 2.0])
    start = datetime(2026, 8, 21, tzinfo=UTC)
    with pytest.raises(RuntimeFailure, match="did not contain"):
        wait_for_correlated_traces(
            Query(),
            resource_id="opaque",
            project="aiq-20260821",
            agent="agent",
            version="v1",
            expectations=[TelemetryExpectation("invoke", None, None, "terra-deployment")],
            start=start,
            end=start + timedelta(minutes=1),
            timeout_seconds=1,
            poll_seconds=0,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(ticks),
        )
