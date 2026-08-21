from __future__ import annotations

import json
import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from agent_insights_quality.cleanup.runtime import cleanup_owned_resources
from agent_insights_quality.insights.client import (
    AgentInsightsClient,
    HttpResponse,
    InsightCheckpoint,
    UrlLibTransport,
)
from agent_insights_quality.insights.telemetry import (
    TelemetryExpectation,
    correlate_complete_traces,
    correlation_query,
    wait_for_correlated_traces,
)
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.receipts import MonitorOwnershipRegistry, read_receipt


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


def registry(tmp_path: Path) -> MonitorOwnershipRegistry:
    return MonitorOwnershipRegistry(tmp_path / "monitor-receipts.json", "private-project-id")


def test_client_sends_real_bearer_header_without_exposing_it_in_errors() -> None:
    transport = FakeTransport([response({"data": [], "has_more": False})])
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=transport,
    )
    client.list_monitors()
    assert transport.requests[0][2]["Authorization"] == "Bear" + "er test-secret-token-value"
    assert "test-secret" not in repr(client)


def test_default_transport_disables_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = UrlLibTransport()

    def redirect(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://project.example.invalid/path",
            302,
            "Found",
            {"Location": "https://evil.invalid"},
            None,
        )

    monkeypatch.setattr(transport._opener, "open", redirect)
    result = transport.request(
        "GET",
        "https://project.example.invalid/path",
        headers={"Authorization": "secret"},
        json_body=None,
        timeout=1,
    )
    assert result.status == 302


def test_client_paginates_with_has_more_and_after_and_fetches_details() -> None:
    transport = FakeTransport(
        [
            response({"data": [{"id": "r1"}], "has_more": True, "last_id": "r1"}),
            response({"data": [{"id": "r2"}], "has_more": False}),
            response({"data": [{"id": "i1"}], "has_more": True, "last_id": "i1"}),
            response({"data": [{"id": "i2"}], "has_more": False}),
            response({"id": "i1", "revision": "1", "updated_at": "2026-08-21T01:00:00Z"}),
            response({"id": "i2", "revision": "1", "updated_at": "2026-08-21T01:00:00Z"}),
        ]
    )
    client = AgentInsightsClient("https://project.example.invalid", Credential(), transport=transport)
    assert [item["id"] for item in client.list_runs("monitor")] == ["r1", "r2"]
    second_query = parse_qs(urlparse(transport.requests[1][1]).query)
    assert second_query["after"] == ["r1"]
    details = client.list_insights("monitor")
    assert [item["id"] for item in details] == ["i1", "i2"]
    assert all("include_details=true" in request[1] for request in transport.requests[-2:])


def test_pagination_fails_closed_without_cursor() -> None:
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=FakeTransport([response({"data": [{"id": "not-a-cursor"}], "has_more": True})]),
    )
    with pytest.raises(RuntimeFailure, match="cursor"):
        client.list_runs("monitor")


def test_run_contract_uses_lookback_only_and_real_cancel_route() -> None:
    transport = FakeTransport(
        [
            response({"id": "run"}, 201),
            HttpResponse(204, {}, b""),
        ]
    )
    client = AgentInsightsClient("https://project.example.invalid", Credential(), transport=transport)
    client.create_run("monitor", lookback_hours=24)
    assert transport.requests[0][3] == {"lookback_hours": 24}
    client.cancel_run("monitor", "run")
    assert "/runs/run:cancel?" in transport.requests[1][1]


def test_monitor_ownership_is_external_and_reset_uses_action_route(tmp_path: Path) -> None:
    ownership = registry(tmp_path)
    transport = FakeTransport(
        [
            response({"data": [{"id": "agent-id", "name": "agent"}], "has_more": False}),
            response({"id": "monitor", "agent_name": "agent", "model_deployment_name": "terra"}, 201),
            response({"id": "monitor", "status": "ready"}, 200),
        ]
    )
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=transport,
        ownership_registry=ownership,
    )
    created = client.create_monitor(
        agent_name="agent",
        model_deployment_name="terra",
        expires_on=date(2026, 8, 28),
    )
    assert "metadata" not in transport.requests[1][3]
    ownership.require(
        agent_name="agent",
        monitor_id=str(created["id"]),
        model_deployment_name="terra",
    )
    client.reset_monitor("monitor", "agent")
    assert "/agent_insight_monitors/monitor:reset?" in transport.requests[2][1]
    receipt = read_receipt(tmp_path / "monitor-receipts.json")
    assert "private-project-id" not in json.dumps(receipt)
    assert '"monitor_reference": "monitor"' not in json.dumps(receipt)


def test_monitor_creation_rolls_back_when_ownership_receipt_fails() -> None:
    class FailingRegistry:
        def record(self, **_kwargs):
            raise RuntimeFailure("monitor_receipt_write_failed", "Synthetic receipt failure.")

    transport = FakeTransport(
        [
            response({"data": [{"id": "agent-id", "name": "agent"}], "has_more": False}),
            response({"id": "monitor"}, 201),
            HttpResponse(204, {}, b""),
        ]
    )
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=transport,
        ownership_registry=FailingRegistry(),
    )
    with pytest.raises(RuntimeFailure, match="Synthetic receipt failure"):
        client.create_monitor(
            agent_name="agent",
            model_deployment_name="terra",
            expires_on=date(2026, 8, 28),
        )
    assert transport.requests[-1][0] == "DELETE"
    assert "/agent_insight_monitors/monitor?" in transport.requests[-1][1]


def test_monitor_cleanup_uses_receipt_expiry(tmp_path: Path) -> None:
    ownership = registry(tmp_path)
    ownership.record(
        agent_name="agent",
        monitor_id="expired",
        model_deployment_name="terra",
        expires_on=date(2026, 8, 20),
    )
    transport = FakeTransport(
        [
            response(
                {
                    "data": [{"id": "expired", "agent_name": "agent"}],
                    "has_more": False,
                }
            ),
            HttpResponse(204, {}, b""),
        ]
    )
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=transport,
        ownership_registry=ownership,
    )
    assert client.cleanup_owned_monitors(now=date(2026, 8, 21), dry_run=False) == ["expired"]
    with pytest.raises(RuntimeFailure, match="no matching"):
        ownership.require(agent_name="agent", monitor_id="expired")


def test_monitor_cleanup_processes_peers_before_reporting_delete_failure(tmp_path: Path) -> None:
    ownership = registry(tmp_path)
    for monitor_id in ("first", "second"):
        ownership.record(
            agent_name="agent",
            monitor_id=monitor_id,
            model_deployment_name="terra",
            expires_on=date(2026, 8, 20),
        )
    transport = FakeTransport(
        [
            response(
                {
                    "data": [
                        {"id": "first", "agent_name": "agent"},
                        {"id": "second", "agent_name": "agent"},
                    ],
                    "has_more": False,
                }
            ),
            response({"error": "synthetic"}, status=500),
            HttpResponse(204, {}, b""),
        ]
    )
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=transport,
        ownership_registry=ownership,
    )
    with pytest.raises(RuntimeFailure, match="other eligible monitors were processed") as caught:
        client.cleanup_owned_monitors(now=date(2026, 8, 21), dry_run=False)
    assert caught.value.details == {"deleted_count": 1, "failure_count": 1}
    ownership.require(agent_name="agent", monitor_id="first")
    with pytest.raises(RuntimeFailure, match="no matching"):
        ownership.require(agent_name="agent", monitor_id="second")


def test_cleanup_coordinator_accepts_concrete_monitor_client_signature(
    tmp_path: Path,
) -> None:
    ownership = registry(tmp_path)
    ownership.record(
        agent_name="agent",
        monitor_id="expired",
        model_deployment_name="terra",
        expires_on=date(2000, 1, 1),
    )
    client = AgentInsightsClient(
        "https://project.example.invalid",
        Credential(),
        transport=FakeTransport(
            [
                response(
                    {
                        "data": [{"id": "expired", "agent_name": "agent"}],
                        "has_more": False,
                    }
                ),
                HttpResponse(204, {}, b""),
            ]
        ),
        ownership_registry=ownership,
    )

    class Projects:
        def cleanup_expired(self, *, now=None, dry_run=True):
            return ["project"]

    class Connections:
        def cleanup_owned_connections(self, owner_reference, *, dry_run=True):
            assert owner_reference == "owner"
            return ["connection"]

    class Artifacts:
        def cleanup_expired(self, owner_reference, *, dry_run=True):
            assert owner_reference == "owner"
            return ["artifact"]

    result = cleanup_owned_resources(
        owner_reference="owner",
        projects=Projects(),
        connections=Connections(),
        monitors=client,
        artifacts=Artifacts(),
        dry_run=False,
    )
    assert result.monitors == ("expired",)


def test_run_window_and_checkpoint_scope_insights_fail_closed() -> None:
    start = datetime(2026, 8, 21, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    run = {
        "id": "run",
        "status": "succeeded",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }
    assert AgentInsightsClient.validate_run_window(run, start, end) == (start, end)
    with pytest.raises(RuntimeFailure, match="different analysis window"):
        AgentInsightsClient.validate_run_window(run, start + timedelta(seconds=1), end)

    checkpoint = InsightCheckpoint(
        captured_at=start + timedelta(minutes=1),
        revisions={"old": "1", "changed": "1"},
    )
    insights = [
        {"id": "old", "revision": "1", "updated_at": (start + timedelta(minutes=2)).isoformat()},
        {"id": "changed", "revision": "2", "updated_at": (start + timedelta(minutes=2)).isoformat()},
        {"id": "new", "revision": "1", "created_at": (start + timedelta(minutes=3)).isoformat()},
    ]
    selected = AgentInsightsClient.scope_insights(insights, checkpoint, start, end)
    assert [item["id"] for item in selected] == ["changed", "new"]
    with pytest.raises(RuntimeFailure, match="timestamp is missing"):
        AgentInsightsClient.scope_insights(
            [{"id": "new", "revision": "1"}],
            checkpoint,
            start,
            end,
        )


def test_run_scope_enforces_five_insight_gate() -> None:
    start = datetime(2026, 8, 21, 1, tzinfo=UTC)
    insights = [
        {
            "id": f"i{index}",
            "revision": "1",
            "created_at": (start + timedelta(minutes=2)).isoformat(),
        }
        for index in range(6)
    ]
    with pytest.raises(RuntimeFailure, match="more than five"):
        AgentInsightsClient.scope_insights(
            insights,
            InsightCheckpoint(start + timedelta(minutes=1), {}),
            start,
            start + timedelta(hours=1),
        )


def telemetry_rows(start: datetime):
    trace = "a" * 32
    common = {
        "timestamp": start + timedelta(seconds=1),
        "operation_id": trace,
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


def test_telemetry_query_uses_supported_agent_fields_not_project_dimension() -> None:
    query = correlation_query(
        agent="agent",
        version="v1",
        expectations=[TelemetryExpectation("invoke", None, None, "terra-deployment")],
    )
    assert "gen_ai.project.name" not in query
    assert 'customDimensions["gen_ai.agent.name"]' in query
    assert 'customDimensions["gen_ai.agent.version"]' in query


def test_correlates_ids_to_w3c_operation_and_requires_complete_parent_chain() -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    expectation = TelemetryExpectation("invoke-1", "response-1", "session-1", "terra-deployment")
    result = correlate_complete_traces(
        telemetry_rows(start),
        [expectation],
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
        agent="agent",
        version="v1",
        start=start,
        end=start + timedelta(minutes=1),
    ) is None
    disconnected = telemetry_rows(start) + [
        telemetry_rows(start)[1] | {"span_id": "cycle-a", "parent_id": "cycle-b"},
        telemetry_rows(start)[1] | {"span_id": "cycle-b", "parent_id": "cycle-a"},
    ]
    assert correlate_complete_traces(
        disconnected,
        [expectation],
        agent="agent",
        version="v1",
        start=start,
        end=start + timedelta(minutes=1),
    ) is None


def test_correlation_prefers_invocation_ids_when_sessions_are_shared() -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    second = [
        row | {"operation_id": "b" * 32, "invocation_id": "invoke-2", "response_id": "response-2"}
        for row in telemetry_rows(start)
    ]
    result = correlate_complete_traces(
        telemetry_rows(start) + second,
        [
            TelemetryExpectation("invoke-1", "response-1", "session-1", "terra-deployment"),
            TelemetryExpectation("invoke-2", "response-2", "session-1", "terra-deployment"),
        ],
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
