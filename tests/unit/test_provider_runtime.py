import asyncio
import builtins
import copy
import hashlib
import inspect
import io
import http.client
import json
import sys
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_insights_quality.contracts import (
    CloudPort,
    Deployment,
    Environment,
    QueryResult,
    Step,
    Target,
    SolPort,
)
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers import (
    AzureLogsReader,
    AzureHttpTransport,
    AzureRuntime,
    AzureSol,
    HttpResponse,
    HttpRequest,
    SolResponseError,
    resolve_environment,
)
from agent_insights_quality.providers.telemetry import time_bounds
from agent_insights_quality.providers.transport import (
    ARM_SCOPE,
    FOUNDRY_SCOPE,
    JsonClient,
)
from agent_insights_quality.results import UnitId


def response(value=None, status=200, headers=None):
    return HttpResponse(
        status, headers or {}, json.dumps(value).encode() if value is not None else b""
    )


class FakeTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        assert self.replies, "Unexpected extra provider request"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def environment():
    return Environment(
        "staging",
        "example",
        "demo",
        "https://example.invalid/api/projects/demo",
        "/synthetic/telemetry",
        "examplestorage",
        "exampleregistry",
        "swedencentral",
        "SwedenCentral",
    )


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "issue"
    root.mkdir()
    (root / "definition.json").write_text(
        json.dumps(
            {
                "definition": {
                    "kind": "prompt",
                    "model": "gpt-5.4-mini",
                    "instructions": "Synthetic example",
                }
            }
        )
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    return Target(
        UnitId("example-agent", "issue-001"),
        "prompt",
        "deterministic",
        root,
        baseline,
        {},
    )


def deployed(target, profile="staging", **details):
    return Deployment(
        target.key,
        target.runtime_name(profile),
        "42",
        target.agent_type,
        "revision-one",
        {"provisioning_state": "active", **details},
    )


def runtime(environment, transport, **kwargs):
    return AzureRuntime(
        environment,
        transport=transport,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        **kwargs,
    )


def test_prompt_create_wire_and_persist_before_return(environment, target):
    transport = FakeTransport(
        response(status=404),
        response({"versions": {"latest": {"version": "42"}}, "name": "example"}, 201),
    )
    saved = []
    result = run(
        runtime(environment, transport).ensure_deployment(
            target, "revision-one", None, saved.append
        )
    )
    assert result.provider_version == "42"
    assert saved[0].details["provisioning_state"] == "submitting"
    assert saved[1].provider_version == "42"
    assert saved[-1].details["provisioning_state"] == "active"
    wire = transport.requests[-1]
    assert wire.url == environment.project_endpoint + "/agents?api-version=v1"
    assert wire.scope == FOUNDRY_SCOPE
    assert "Foundry-Features" not in wire.headers
    body = json.loads(wire.body)
    assert body["name"] == "example-agent-issue-001"
    assert body["definition"]["kind"] == "prompt"
    assert body["metadata"] == {
        "aiq_profile": "staging",
        "aiq_logical_version": "issue-001",
        "aiq_source_revision": "revision-one",
    }


def test_registry_exact_version_does_not_list_or_use_latest(environment, target):
    transport = FakeTransport(response({"version": "42"}))
    saved = []
    result = run(
        runtime(environment, transport).ensure_deployment(
            target, "revision-one", deployed(target), saved.append
        )
    )
    assert result.provider_version == "42"
    assert len(transport.requests) == 1
    assert "/versions/42?" in transport.requests[0].url


def test_exact_version_read_requires_real_returned_version(environment, target):
    transport = FakeTransport(response({"status": "active"}))
    with pytest.raises(QualityError, match="deployment_version_missing"):
        run(runtime(environment, transport).ensure_deployment(
            target, "revision-one", deployed(target), lambda value: None,
        ))


def test_hosted_budget_is_forwarded_from_the_reviewed_request(environment, target):
    hosted = replace(target, agent_type="hosted_custom_container")
    transport = FakeTransport(response({"id": "response-one", "status": "completed"}))
    step = Step("probe", "probe", {"input": "Synthetic request", "max_output_tokens": 400}, {})
    run(runtime(environment, transport).invoke(
        deployed(hosted), step, request_id="request-one", session_id="session-one",
        previous_response_id=None, persist=lambda value: None,
    ))
    assert json.loads(transport.requests[0].body)["max_output_tokens"] == 400


def test_missing_ready_version_is_recreated_only_after_listing(environment, target):
    transport = FakeTransport(
        response(status=404),
        response({"name": "example-agent"}),
        response({"data": []}),
        response({"version": "43"}, 201),
    )
    result = run(
        runtime(environment, transport).ensure_deployment(
            target, "revision-one", deployed(target), lambda value: None
        )
    )
    assert result.provider_version == "43"
    assert transport.requests[-1].url.endswith("/versions?api-version=v1")


def test_hosted_multipart_contains_complete_selected_source(environment, target):
    target = replace(target, agent_type="hosted_code")
    (target.version_root / "source").mkdir()
    (target.version_root / "source" / "app.py").write_text("selected = True\n")
    (target.version_root / "source" / "new_module.py").write_text("new = True\n")
    (target.version_root / "source" / "__pycache__").mkdir()
    (target.version_root / "source" / "__pycache__" / "cache.pyc").write_bytes(b"cache")
    (target.baseline_root / "source").mkdir()
    (target.baseline_root / "source" / "baseline_only.py").write_text("wrong = True\n")
    (target.baseline_root / "host.yaml").write_text(
        "entrypoint: python -m source.app\n"
    )
    (target.baseline_root / "requirements.txt").write_text("synthetic-package==1.0\n")
    transport = FakeTransport(
        response(status=404), response({"version": "42", "status": "active"}, 201)
    )
    saved = []
    run(
        runtime(environment, transport).ensure_deployment(
            target, "revision-one", None, saved.append
        )
    )
    wire = transport.requests[-1]
    assert wire.headers["Foundry-Features"] == "HostedAgents=V1Preview"
    assert wire.headers["x-ms-agent-name"] == target.runtime_name("staging")
    boundary = wire.headers["Content-Type"].split("boundary=")[1].encode()
    sections = wire.body.split(b"--" + boundary)
    metadata = json.loads(sections[1].split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n"))
    archive = sections[2].split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")
    assert wire.headers["x-ms-code-zip-sha256"] == hashlib.sha256(archive).hexdigest()
    assert metadata["definition"]["code_configuration"]["entry_point"] == [
        "python",
        "-m",
        "source.app",
    ]
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        assert set(package.namelist()) == {
            "source/app.py",
            "source/new_module.py",
            "host.yaml",
            "requirements.txt",
        }
        assert (
            package.read("source/app.py")
            == (target.version_root / "source" / "app.py").read_bytes()
        )


def test_pending_create_preserves_identity_and_resume_reads_exact(environment, target):
    target = replace(target, agent_type="hosted_code")
    # Reconcile from provider metadata without requiring local packaging.
    metadata = {
        "aiq_profile": "staging",
        "aiq_logical_version": "issue-001",
        "aiq_source_revision": "r",
    }
    transport = FakeTransport(
        response({}),
        response(
            {"data": [{"version": "42", "status": "creating", "metadata": metadata}]}
        ),
        response({"version": "42", "status": "active"}),
    )
    provider = runtime(environment, transport)
    saved = []
    with pytest.raises(QualityError, match="deployment_pending") as error:
        run(provider.ensure_deployment(target, "r", None, saved.append))
    assert error.value.retryable and error.value.request_accepted is True
    assert saved[-1].provider_version == "42"
    result = run(provider.ensure_deployment(target, "r", saved[-1], saved.append))
    assert result.details["provisioning_state"] == "active"
    assert all(wire.method == "GET" for wire in transport.requests)


def test_accepted_create_persists_returned_version_before_pending(environment, target):
    transport = FakeTransport(
        response(status=404),
        response({"version": "42"}, 202, {"Operation-Location": "/native/poll"}),
    )
    saved = []
    with pytest.raises(QualityError, match="deployment_pending"):
        run(
            runtime(environment, transport).ensure_deployment(
                target, "r", None, saved.append
            )
        )
    assert saved[-1].provider_version == "42"
    assert saved[-1].details["polling_url"] == "/native/poll"


def test_pending_known_version_404_never_creates(environment, target):
    transport = FakeTransport(response(status=404))
    pending = deployed(target, provisioning_state="pending")
    with pytest.raises(QualityError, match="deployment_propagation_pending"):
        run(
            runtime(environment, transport).ensure_deployment(
                target, "revision-one", pending, lambda x: None
            )
        )
    assert len(transport.requests) == 1


def test_unknown_create_is_checkpointed_and_not_resubmitted(environment, target):
    transport = FakeTransport(
        response(status=404),
        TimeoutError("synthetic unsafe diagnostic"),
        response(status=404),
    )
    provider = runtime(environment, transport)
    saved = []
    with pytest.raises(QualityError, match="provider_no_response") as error:
        run(provider.ensure_deployment(target, "r", None, saved.append))
    assert error.value.request_accepted is None
    assert not error.value.retryable
    assert "unsafe" not in str(error.value)
    assert saved[-1].details["provisioning_state"] == "unknown"
    with pytest.raises(QualityError, match="deployment_create_unresolved"):
        run(provider.ensure_deployment(target, "r", saved[-1], saved.append))
    assert [wire.method for wire in transport.requests].count("POST") == 1


def test_version_recovery_reads_past_one_hundred(environment, target):
    metadata = {
        "aiq_profile": "staging",
        "aiq_logical_version": "issue-001",
        "aiq_source_revision": "r",
    }
    transport = FakeTransport(
        response({}),
        response(
            {
                "data": [{"version": str(i)} for i in range(100)],
                "nextLink": "?after=100",
            }
        ),
        response({"data": [{"version": "151", "metadata": metadata}]}),
    )
    result = run(
        runtime(environment, transport).ensure_deployment(
            target, "r", None, lambda x: None
        )
    )
    assert result.provider_version == "151"
    assert "after=100" in transport.requests[-1].url
    assert all(wire.method == "GET" for wire in transport.requests)


def test_ambiguous_matching_versions_are_not_selected(environment, target):
    metadata = {
        "aiq_profile": "staging",
        "aiq_logical_version": "issue-001",
        "aiq_source_revision": "r",
    }
    transport = FakeTransport(
        response({}),
        response(
            {
                "data": [
                    {"version": "1", "metadata": metadata},
                    {"version": "2", "metadata": metadata},
                ]
            }
        ),
    )
    with pytest.raises(QualityError, match="deployment_versions_ambiguous"):
        run(
            runtime(environment, transport).ensure_deployment(
                target, "r", None, lambda x: None
            )
        )


def test_checkpoint_failure_prevents_create(environment, target):
    transport = FakeTransport(response(status=404))

    def fail(value):
        raise OSError("synthetic disk failure")

    with pytest.raises(QualityError, match="provider_checkpoint_failed"):
        run(runtime(environment, transport).ensure_deployment(target, "r", None, fail))
    assert [wire.method for wire in transport.requests] == ["GET"]


def test_activation_confirms_exact_fixed_ratio(environment, target):
    target = replace(target, agent_type="hosted_code")
    expected = {
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [
                    {
                        "agent_version": "42",
                        "traffic_percentage": 100,
                        "type": "FixedRatio",
                    }
                ]
            }
        }
    }
    transport = FakeTransport(response(expected))
    run(runtime(environment, transport).activate(deployed(target)))
    wire = transport.requests[0]
    assert wire.method == "PATCH"
    assert json.loads(wire.body) == expected
    assert wire.headers["Content-Type"] == "application/merge-patch+json"


@pytest.mark.parametrize("field", ["id", "session_id", "agent_session_id"])
def test_actual_hosted_session_is_persisted(environment, target, field):
    target = replace(target, agent_type="hosted_code")
    transport = FakeTransport(
        response(
            {
                field: "actual-session",
                "version_indicator": {"type": "version_ref", "agent_version": "42"},
            },
            201,
        )
    )
    saved = []
    result = run(
        runtime(environment, transport).create_session(
            deployed(target), "client-request", saved.append
        )
    )
    assert result == "actual-session" and saved == [result]
    wire = transport.requests[0]
    assert json.loads(wire.body) == {
        "version_indicator": {"type": "version_ref", "agent_version": "42"}
    }
    assert "/endpoint/sessions?api-version=v1" in wire.url


def test_session_binding_error_still_preserves_actual_identity(environment, target):
    target = replace(target, agent_type="hosted_code")
    transport = FakeTransport(
        response(
            {
                "id": "actual-session",
                "version_indicator": {"type": "version_ref", "agent_version": "99"},
            }
        )
    )
    saved = []
    with pytest.raises(QualityError, match="session_version_mismatch") as error:
        run(
            runtime(environment, transport).create_session(
                deployed(target), "request", saved.append
            )
        )
    assert saved == ["actual-session"] and error.value.request_accepted is True


def test_prompt_exact_version_previous_response_chain_and_full_result(
    environment, target
):
    output = {
        "id": "actual-response",
        "status": "completed",
        "output": [{"extra": "unprojected"}],
        "usage": {"output_tokens": 4},
        "new_provider_field": {"opaque": [1, 2]},
    }
    transport = FakeTransport(response(output))
    saved = []
    result = run(
        runtime(environment, transport).invoke(
            deployed(target),
            Step("turn", "main", {"input": "synthetic input"}, {}),
            request_id="client-id",
            session_id=None,
            previous_response_id="previous-actual-id",
            persist=saved.append,
        )
    )
    assert saved[0].status == "submitting" and saved[-1] == result
    assert result.request_id == "client-id" and result.response_id == "actual-response"
    assert result.response == output
    wire = transport.requests[0]
    assert wire.url == environment.project_endpoint + "/openai/v1/responses"
    assert wire.headers["x-ms-client-request-id"] == "client-id"
    assert json.loads(wire.body) == {
        "input": "synthetic input",
        "store": True,
        "previous_response_id": "previous-actual-id",
        "agent_reference": {
            "type": "agent_reference",
            "name": "example-agent-issue-001",
            "version": "42",
        },
    }


def test_hosted_invoke_routes_actual_session(environment, target):
    target = replace(target, agent_type="hosted_code")
    transport = FakeTransport(
        response({"id": "actual-response", "status": "completed", "output": []})
    )
    run(
        runtime(environment, transport).invoke(
            deployed(target),
            Step(
                "turn",
                "main",
                {"input": [{"role": "user", "content": "synthetic"}]},
                {},
            ),
            request_id="request",
            session_id="actual-session",
            previous_response_id=None,
            persist=lambda x: None,
        )
    )
    wire = transport.requests[0]
    assert "/endpoint/protocols/openai/responses?api-version=v1" in wire.url
    assert wire.headers["Foundry-Features"] == "HostedAgents=V1Preview"
    assert json.loads(wire.body) == {
        "input": [{"role": "user", "content": "synthetic"}],
        "agent_session_id": "actual-session",
        "store": False,
    }


@pytest.mark.parametrize(
    "reply,accepted",
    [
        (TimeoutError("synthetic private diagnostic"), None),
        (response({"error": {"message": "synthetic private diagnostic"}}, 503), None),
        (response({"error": {"message": "synthetic private diagnostic"}}, 429), False),
    ],
)
def test_invocation_unknown_post_is_never_retried(environment, target, reply, accepted):
    transport = FakeTransport(reply)
    saved = []
    with pytest.raises(QualityError) as error:
        run(
            runtime(environment, transport).invoke(
                deployed(target),
                Step("t", "main", {"input": "synthetic"}, {}),
                request_id="request",
                session_id=None,
                previous_response_id=None,
                persist=saved.append,
            )
        )
    assert error.value.request_accepted is accepted
    assert "private diagnostic" not in str(error.value)
    assert len(transport.requests) == 1 and len(saved) == 2
    assert saved[-1].status == ("failed" if accepted is False else "unknown")


def test_invocation_incomplete_is_not_a_completed_response(environment, target):
    value = {
        "id": "response",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    transport = FakeTransport(response(value))
    result = run(
        runtime(environment, transport).invoke(
            deployed(target),
            Step("t", "main", {"input": "synthetic"}, {}),
            request_id="request",
            session_id=None,
            previous_response_id=None,
            persist=lambda x: None,
        )
    )
    assert result.status == "incomplete"
    assert result.response == value


def test_daily_monitors_are_agent_scoped_and_paginated(environment):
    environment = replace(environment, profile="daily")
    transport = FakeTransport(
        response(
            {
                "data": [{"id": "other", "agent_name": "other"}],
                "next_link": "?after=other",
            }
        ),
        response({"data": [{"id": "native-monitor", "agent_name": "example-agent"}]}),
    )
    assert (
        run(runtime(environment, transport).ensure_monitor("example-agent"))
        == "native-monitor"
    )
    assert len(transport.requests) == 2 and all(
        wire.method == "GET" for wire in transport.requests
    )


def test_missing_monitor_create_has_no_version_filter(environment):
    transport = FakeTransport(response({"data": []}), response({"id": "monitor"}, 201))
    assert (
        run(
            runtime(replace(environment, profile="daily"), transport).ensure_monitor(
                "agent"
            )
        )
        == "monitor"
    )
    assert json.loads(transport.requests[-1].body) == {
        "agent_name": "agent",
        "enabled": False,
        "run_interval_hours": 24,
        "model_deployment_name": "terra-insight-generation",
    }


@pytest.mark.parametrize(
    "method,args",
    [
        ("ensure_monitor", ("agent",)),
        ("reset_monitor", ("monitor",)),
        ("start_insights", ("monitor", 0.012345, "operation", lambda x: None)),
        ("get_insights_run", ("monitor", "run")),
        ("list_insights", ("monitor",)),
    ],
)
def test_staging_cannot_touch_insights(environment, method, args):
    transport = FakeTransport()
    with pytest.raises(QualityError, match="insights_daily_only"):
        run(getattr(runtime(environment, transport), method)(*args))
    assert not transport.requests


def test_fractional_insights_lookback_native_operation_and_exact_retry(environment):
    transport = FakeTransport(
        TimeoutError("synthetic timeout"),
        response(
            {"id": "actual-run", "status": "queued"},
            202,
            {"Operation-Location": "/native/poll"},
        ),
    )
    provider = runtime(replace(environment, profile="daily"), transport)
    saved = []
    with pytest.raises(QualityError):
        run(
            provider.start_insights(
                "monitor", 0.01234567, "stable-operation", saved.append
            )
        )
    assert saved[-1]["submission_state"] == "unknown"
    result = run(
        provider.start_insights("monitor", 0.01234567, "stable-operation", saved.append)
    )
    first, second = transport.requests
    assert first.body == second.body == b'{"lookback_hours":0.01234567}'
    assert (
        first.headers["Operation-Id"]
        == second.headers["Operation-Id"]
        == "stable-operation"
    )
    assert result["id"] == "actual-run" and result["polling_url"] == "/native/poll"
    assert saved[-1]["submission_state"] == "accepted"
    with pytest.raises(QualityError, match="insights_operation_body_changed"):
        run(provider.start_insights("monitor", 0.02, "stable-operation", saved.append))
    assert len(transport.requests) == 2


def test_insights_checkpoint_failure_prevents_admission(environment):
    transport = FakeTransport()

    def fail(value):
        raise OSError("synthetic disk failure")

    with pytest.raises(QualityError, match="provider_checkpoint_failed"):
        run(
            runtime(replace(environment, profile="daily"), transport).start_insights(
                "m", 0.01, "op", fail
            )
        )
    assert not transport.requests


def test_reset_accepted_is_not_silently_completed_or_retried(environment):
    transport = FakeTransport(response(status=202))
    with pytest.raises(QualityError, match="insights_reset_pending") as error:
        run(
            runtime(replace(environment, profile="daily"), transport).reset_monitor(
                "monitor"
            )
        )
    assert error.value.request_accepted is True and not error.value.retryable
    assert len(transport.requests) == 1


def test_card_pages_preserve_cumulative_links_and_actual_count(environment):
    card = {
        "id": "card",
        "trace_count": 10000,
        "details": {
            "linked_traces": [{"trace_id": "historical"}, {"trace_id": "current"}],
            "highlighted_traces": [{"trace_id": "current"}],
            "new_detail": {"unprojected": True},
        },
    }
    revised = {**card, "updated_at": "later"}
    transport = FakeTransport(
        response({"data": [card], "nextLink": "?after=card"}),
        response({"data": [revised]}),
    )
    result = run(
        runtime(replace(environment, profile="daily"), transport).list_insights(
            "monitor"
        )
    )
    assert result == (card, revised)
    assert "run_id" not in result[0]
    assert all("include_details=true" in wire.url for wire in transport.requests)


def test_pagination_rejects_foreign_origin_without_sending(environment):
    transport = FakeTransport(
        response({"data": [], "nextLink": "https://untrusted.invalid/data"})
    )
    with pytest.raises(QualityError, match="provider_link_out_of_scope"):
        run(JsonClient(environment.project_endpoint, transport).pages("/agents"))
    assert len(transport.requests) == 1


def test_pagination_cycle_is_explicit(environment):
    url = environment.project_endpoint + "/agents?api-version=v1"
    transport = FakeTransport(response({"data": [], "nextLink": url}))
    with pytest.raises(QualityError, match="provider_pagination_cycle"):
        run(JsonClient(environment.project_endpoint, transport).pages("/agents"))


def test_get_retries_are_bounded_and_injected(environment):
    waits = []

    async def sleep(delay):
        waits.append(delay)

    transport = FakeTransport(
        response(status=503), TimeoutError(), response({"data": []})
    )
    assert (
        run(
            JsonClient(environment.project_endpoint, transport, sleep=sleep).pages(
                "/agents"
            )
        )
        == []
    )
    assert waits == [1, 2]


def test_telemetry_preserves_tables_columns_values_and_partial_state():
    table = SimpleNamespace(
        name="customLogs",
        columns=["message", "duplicate", "duplicate"],
        rows=[["full synthetic payload", {"nested": [1, 2]}, "other"]],
    )
    success = AzureLogsReader.convert(SimpleNamespace(status="Success", tables=[table]))
    partial = AzureLogsReader.convert(
        SimpleNamespace(
            status="PartialError",
            partial_data=[table],
            partial_error={"code": "PartialQueryFailure"},
        )
    )
    assert success.complete and not partial.complete
    assert partial.error_code == "telemetry_query_partial"
    assert (
        success.records
        == partial.records
        == (
            {
                "table": "customLogs",
                "columns": [
                    {"name": "message"},
                    {"name": "duplicate"},
                    {"name": "duplicate"},
                ],
                "values": ["full synthetic payload", {"nested": [1, 2]}, "other"],
            },
        )
    )


def test_query_delegates_unmodified_kql_bounds_and_partial_result(environment):
    expected = QueryResult(({"raw": [1, 2, 3]},), False, "telemetry_query_partial")
    calls = []

    class Logs:
        async def query(self, query, *, start, end):
            calls.append((query, start, end))
            return expected

    provider = runtime(environment, FakeTransport(), logs=Logs())
    assert run(provider.query("arbitrary KQL", start="start", end="end")) is expected
    assert calls == [("arbitrary KQL", "start", "end")]


@pytest.mark.parametrize(
    "start,end",
    [
        ("invalid", "invalid"),
        ("2026-01-01", "2026-01-02"),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"),
    ],
)
def test_telemetry_invalid_boundaries_are_explicit(start, end):
    with pytest.raises(QualityError, match="telemetry_time_invalid"):
        time_bounds(start, end)


SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def sol_output(text='{"verdict":"synthetic"}', **extra):
    return {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        **extra,
    }


def test_structured_sol_exact_deployment_schema_and_bounded_rejection_retry(
    environment,
):
    waits = []

    async def sleep(delay):
        waits.append(delay)

    transport = FakeTransport(
        response({"error": "rate limited"}, 429, {"Retry-After": "2"}),
        response(sol_output()),
    )
    sol = AzureSol(environment, transport=transport, sleep=sleep)
    result = run(
        sol.complete_json(
            instructions="Judge synthetic input",
            payload={"evidence": [1]},
            schema=SCHEMA,
        )
    )
    assert result == {"verdict": "synthetic"} and waits == [2]
    first, second = transport.requests
    assert first.body == second.body
    body = json.loads(first.body)
    assert body["model"] == "sol-assessment"
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "assessment",
        "strict": True,
        "schema": SCHEMA,
    }
    assert json.loads(body["input"]) == {"evidence": [1]}
    assert body["store"] is False
    assert first.url == environment.project_endpoint + "/openai/v1/responses"


@pytest.mark.parametrize(
    "value,code",
    [
        (
            sol_output(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            ),
            "sol_response_incomplete",
        ),
        (
            sol_output(status="failed", error={"message": "synthetic private error"}),
            "sol_response_failed",
        ),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "refusal", "refusal": "synthetic refusal"}
                        ],
                    }
                ],
            },
            "sol_refused",
        ),
        (sol_output('{"unexpected":true}'), "sol_output_schema_invalid"),
        (sol_output("not json"), "sol_output_schema_invalid"),
    ],
)
def test_sol_errors_keep_private_response_but_only_safe_exception_text(
    environment, value, code
):
    sol = AzureSol(environment, transport=FakeTransport(response(value)))
    with pytest.raises(SolResponseError) as error:
        run(sol.complete_json(instructions="synthetic", payload={}, schema=SCHEMA))
    assert str(error.value) == code
    assert error.value.response == value


def test_sol_unknown_post_not_retried(environment):
    transport = FakeTransport(TimeoutError("synthetic private error"))
    with pytest.raises(QualityError) as error:
        run(
            AzureSol(environment, transport=transport).complete_json(
                instructions="synthetic", payload={}, schema=SCHEMA
            )
        )
    assert error.value.request_accepted is None
    assert len(transport.requests) == 1


def test_environment_resolution_is_selected_and_read_only():
    account = "/subscriptions/synthetic/resourceGroups/example/providers/Microsoft.CognitiveServices/accounts/example"
    transport = FakeTransport(
        response({"location": "swedencentral"}),
        response(
            {"value": [{"name": "swedencentral", "displayName": "Sweden Central"}]}
        ),
    )
    environment = run(
        resolve_environment(
            profile="daily",
            account_resource_id=account,
            project_name="demo",
            application_insights_resource_id="/synthetic/telemetry",
            storage_account_name="storage",
            registry_name="registry",
            transport=transport,
        )
    )
    assert environment.region_display == "SwedenCentral"
    assert (
        environment.project_endpoint
        == "https://example.services.ai.azure.com/api/projects/demo"
    )
    assert all(
        wire.method == "GET" and wire.scope == ARM_SCOPE for wire in transport.requests
    )
    assert parse_qs(urlsplit(transport.requests[0].url).query) == {
        "api-version": ["2025-06-01"]
    }


def test_provider_composition_does_not_import_azure_sdk(environment, monkeypatch):
    real_import = builtins.__import__
    imports = []

    def guarded(name, *args, **kwargs):
        if name == "azure" or name.startswith("azure."):
            imports.append(name)
            raise AssertionError("Azure SDK import during offline composition")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    AzureRuntime(environment)
    AzureSol(environment)
    assert not imports
    assert "azure.identity" not in sys.modules


def test_invocation_checkpoint_has_independent_snapshot(environment, target):
    transport = FakeTransport(
        response({"id": "actual-response", "status": "completed"})
    )
    saved = []
    run(
        runtime(environment, transport).invoke(
            deployed(target),
            Step("turn", "main", {"input": "synthetic"}, {}),
            request_id="client",
            session_id=None,
            previous_response_id=None,
            persist=lambda value: saved.append(copy.deepcopy(value)),
        )
    )
    assert saved[0].response is None and saved[1].response_id == "actual-response"


def test_invalid_create_json_retains_unknown_checkpoint(environment, target):
    transport = FakeTransport(
        response(status=404), HttpResponse(201, body=b"malformed")
    )
    saved = []
    with pytest.raises(QualityError, match="provider_invalid_json"):
        run(
            runtime(environment, transport).ensure_deployment(
                target, "r", None, saved.append
            )
        )
    assert saved[-1].details["provisioning_state"] == "unknown"
    assert saved[-1].provider_version == ""


def test_create_without_version_retains_native_polling_reference(environment, target):
    transport = FakeTransport(
        response(status=404), response({}, 202, {"Location": "/native/pending"})
    )
    saved = []
    with pytest.raises(
        QualityError, match="deployment_create_identity_pending"
    ) as error:
        run(
            runtime(environment, transport).ensure_deployment(
                target, "r", None, saved.append
            )
        )
    assert error.value.request_accepted is True
    assert saved[-1].details["polling_url"] == "/native/pending"


@pytest.mark.parametrize(
    "field", ["conversation", "store", "agent_reference", "previous_response_id"]
)
def test_step_cannot_override_provider_identity_or_continuation(
    environment, target, field
):
    transport = FakeTransport()
    with pytest.raises(QualityError, match="invocation_reserved_field"):
        run(
            runtime(environment, transport).invoke(
                deployed(target),
                Step("turn", "main", {"input": "synthetic", field: "override"}, {}),
                request_id="request",
                session_id=None,
                previous_response_id=None,
                persist=lambda x: None,
            )
        )
    assert not transport.requests


def test_header_controls_are_rejected_before_transport(environment):
    transport = FakeTransport()
    with pytest.raises(QualityError, match="provider_header_invalid"):
        run(
            JsonClient(environment.project_endpoint, transport).request(
                "POST",
                "/agents",
                {},
                headers={"x-ms-client-request-id": "synthetic\r\ninvalid"},
            )
        )
    assert not transport.requests


def test_pagination_cursor_retains_original_query(environment):
    transport = FakeTransport(
        response({"data": [{"id": "one"}], "has_more": True}),
        response({"data": [{"id": "two"}], "has_more": False}),
    )
    result = run(
        JsonClient(environment.project_endpoint, transport).pages(
            "/agents?include_details=true"
        )
    )
    assert result == [{"id": "one"}, {"id": "two"}]
    query = parse_qs(urlsplit(transport.requests[-1].url).query)
    assert query == {
        "include_details": ["true"],
        "api-version": ["v1"],
        "after": ["one"],
    }


def test_malformed_page_is_not_an_empty_success(environment):
    transport = FakeTransport(response({}))
    with pytest.raises(QualityError, match="provider_pagination_invalid"):
        run(JsonClient(environment.project_endpoint, transport).pages("/agents"))


def test_primary_result_does_not_replace_actual_telemetry_source():
    result = AzureLogsReader.convert(
        SimpleNamespace(
            status="Success",
            tables=[
                SimpleNamespace(
                    name="PrimaryResult",
                    columns=["telemetry_table", "message"],
                    rows=[["dependencies", "synthetic payload"]],
                )
            ],
        )
    )
    assert result.records[0]["table"] == "PrimaryResult"
    assert result.records[0]["values"] == ["dependencies", "synthetic payload"]
    assert result.records[0]["columns"][0] == {"name": "telemetry_table"}


@pytest.mark.parametrize("status", [400, 429, 503])
def test_sol_http_failures_preserve_private_payload_without_fallback(
    environment, status
):
    value = {"error": {"message": "synthetic private error"}}
    transport = FakeTransport(response(value, status))
    with pytest.raises(SolResponseError, match="sol_http_error") as error:
        run(
            AzureSol(environment, transport=transport, attempts=1).complete_json(
                instructions="synthetic", payload={}, schema=SCHEMA
            )
        )
    assert error.value.response == value
    assert error.value.request_accepted is (None if status == 503 else False)
    assert len(transport.requests) == 1


def test_sol_invalid_response_json_is_explicit_private_diagnostic(environment):
    transport = FakeTransport(HttpResponse(200, body=b"malformed synthetic payload"))
    with pytest.raises(SolResponseError, match="sol_response_invalid_json") as error:
        run(
            AzureSol(environment, transport=transport).complete_json(
                instructions="synthetic", payload={}, schema=SCHEMA
            )
        )
    assert error.value.response == {"raw_body": "malformed synthetic payload"}


def test_sol_schema_is_validated_before_submission(environment):
    transport = FakeTransport()
    with pytest.raises(QualityError, match="sol_schema_invalid"):
        run(
            AzureSol(environment, transport=transport).complete_json(
                instructions="synthetic", payload={}, schema={"type": "not-a-type"}
            )
        )
    assert not transport.requests


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict":"first","verdict":"second"}',
        '{"verdict":NaN}',
    ],
)
def test_sol_ambiguous_or_nonfinite_json_is_rejected(environment, text):
    transport = FakeTransport(response(sol_output(text)))
    with pytest.raises(SolResponseError, match="sol_output_schema_invalid"):
        run(
            AzureSol(environment, transport=transport).complete_json(
                instructions="synthetic", payload={}, schema=SCHEMA
            )
        )


def test_public_adapters_match_exact_port_call_signatures():
    for port, adapter in ((CloudPort, AzureRuntime), (SolPort, AzureSol)):
        for name, contract_method in vars(port).items():
            if not inspect.iscoroutinefunction(contract_method):
                continue
            implementation = getattr(adapter, name)
            assert inspect.iscoroutinefunction(implementation)
            expected = inspect.signature(contract_method).parameters
            actual = inspect.signature(implementation).parameters
            assert tuple(actual) == tuple(expected)
            assert [item.kind for item in actual.values()] == [
                item.kind for item in expected.values()
            ]
            assert [item.default for item in actual.values()] == [
                item.default for item in expected.values()
            ]


def test_native_http_auth_and_incomplete_read_have_no_sensitive_output(
    environment, monkeypatch
):
    class AzureError(Exception):
        pass

    exceptions = ModuleType("azure.core.exceptions")
    exceptions.AzureError = AzureError
    identity = ModuleType("azure.identity")
    identity.AzureCliCredential = lambda: None
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    scopes = []
    wires = []

    class Credential:
        def get_token(self, scope):
            scopes.append(scope)
            return SimpleNamespace(token="synthetic-auth-value")

    class Reply:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"synthetic partial response", 100)

    class Opener:
        def open(self, wire, **kwargs):
            wires.append(wire)
            return Reply()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    request = HttpRequest(
        "POST", environment.project_endpoint, FOUNDRY_SCOPE, body=b"synthetic"
    )
    with pytest.raises(QualityError, match="provider_no_response") as error:
        run(AzureHttpTransport(Credential()).send(request))
    assert error.value.request_accepted is None
    assert scopes == [FOUNDRY_SCOPE]
    assert wires[0].get_header("Authorization") == "Bearer synthetic-auth-value"
    assert "synthetic-auth-value" not in repr(request)
    assert "synthetic partial response" not in str(error.value)
