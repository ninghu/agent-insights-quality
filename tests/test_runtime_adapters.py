from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_insights_quality.contracts import ROOT
from agent_insights_quality.runtime import (
    DIGEST_KEY,
    HOSTED_FEATURES,
    IMAGE_DIGEST_KEY,
    OWNER_KEY,
    OWNER_VALUE,
    RUN_KEY,
    SOURCE_DIGEST_KEY,
    DeploymentReceipt,
    FoundryDeploymentClient,
    FoundryInvocationClient,
    HealthyFixture,
    HttpResponse,
    RuntimeContractError,
    canonical_json_digest,
    deterministic_zip,
    load_fixtures,
    run_healthy_traffic,
    validate_image_reference,
)


PROJECT_ENDPOINT = "https://sample.services.ai.azure.com/api/projects/quality"


def _response(
    status: int,
    value: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers=headers or {},
        body=json.dumps(value or {}, separators=(",", ":")).encode("ascii"),
    )


class QueueTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def request(self, method, url, *, headers, body, timeout_seconds):
        with self.lock:
            self.calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": dict(headers),
                    "body": body,
                    "timeout_seconds": timeout_seconds,
                }
            )
            if not self.responses:
                raise AssertionError(f"Unexpected request: {method} {url}")
            return self.responses.pop(0)


def _metadata(run_id: str, digest: str) -> dict[str, str]:
    return {OWNER_KEY: OWNER_VALUE, RUN_KEY: run_id, DIGEST_KEY: digest}


def test_definition_and_source_hashes_are_deterministic(tmp_path: Path) -> None:
    definition = {"kind": "prompt", "tools": [], "model": "synthetic"}
    assert canonical_json_digest(definition) == canonical_json_digest(
        {"model": "synthetic", "tools": [], "kind": "prompt"}
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('healthy')\n", encoding="ascii")
    (source / "requirements.txt").write_text("example==1.0\n", encoding="ascii")
    first, first_digest = deterministic_zip(source)
    second, second_digest = deterministic_zip(source)
    assert first == second
    assert first_digest == second_digest
    assert first_digest.startswith("sha256:")


def test_prompt_deployment_sends_real_token_and_polls_owned_version() -> None:
    definition = {"kind": "prompt", "model": "test", "instructions": "healthy", "tools": []}
    digest = canonical_json_digest(definition)
    transport = QueueTransport(
        [
            _response(201, {"version": "7"}),
            _response(200, {"status": "active", "metadata": _metadata("run-1", digest)}),
        ]
    )
    client = FoundryDeploymentClient(
        PROJECT_ENDPOINT,
        lambda: "short-lived-token",
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    receipt = client.deploy_prompt(
        agent_name="aiq-001-weather", definition=definition, run_id="run-1"
    )
    assert receipt.agent_version == "7"
    assert receipt.artifact_digest == digest
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer short-lived-token"
    payload = json.loads(transport.calls[0]["body"])
    assert payload["metadata"] == _metadata("run-1", digest)
    assert payload["definition"] == definition
    assert transport.calls[1]["url"].endswith(
        "/agents/aiq-001-weather/versions/7?api-version=v1"
    )


def test_hosted_source_uses_multipart_hash_feature_header_and_timeout() -> None:
    source = ROOT / "agents" / "finance-hosted" / "source"
    _, source_digest = deterministic_zip(source)
    definition = json.loads(
        (ROOT / "agents" / "finance-hosted" / "definition.json").read_text(
            encoding="ascii"
        )
    )
    digest = canonical_json_digest(
        {"definition": definition, "source_digest": source_digest}
    )
    active_metadata = {
        **_metadata("run-2", digest),
        SOURCE_DIGEST_KEY: source_digest,
    }
    transport = QueueTransport(
        [
            _response(201, {"version": "2"}),
            _response(200, {"status": "active", "metadata": active_metadata}),
        ]
    )
    client = FoundryDeploymentClient(
        PROJECT_ENDPOINT,
        lambda: "token",
        transport=transport,
        request_timeout_seconds=37,
        sleeper=lambda _seconds: None,
    )
    receipt = client.deploy_hosted_source(
        agent_name="aiq-003-finance",
        definition=definition,
        source=source,
        run_id="run-2",
    )
    call = transport.calls[0]
    assert receipt.artifact_digest == digest
    assert receipt.source_digest == source_digest
    assert call["headers"]["x-ms-code-zip-sha256"] == source_digest.removeprefix(
        "sha256:"
    )
    assert call["headers"]["Foundry-Features"] == HOSTED_FEATURES
    assert call["headers"]["Content-Type"].startswith("multipart/form-data; boundary=aiq-")
    assert b'name="metadata"' in call["body"]
    assert b'name="code"; filename="aiq-003-finance.zip"' in call["body"]
    assert call["timeout_seconds"] == 37


def test_container_deployment_requires_immutable_public_ghcr_digest() -> None:
    image_digest = "sha256:" + ("a" * 64)
    image = f"ghcr.io/ninghu/agent-insights-quality-ticket@{image_digest}"
    definition = json.loads(
        (
            ROOT / "agents" / "support-ticket-hosted-image" / "definition.json"
        ).read_text(encoding="ascii")
    )
    resolved = json.loads(json.dumps(definition))
    resolved["container_configuration"]["image"] = image
    digest = canonical_json_digest(resolved)
    active_metadata = {
        **_metadata("run-3", digest),
        IMAGE_DIGEST_KEY: image_digest,
    }
    transport = QueueTransport(
        [
            _response(201, {"version": "3"}),
            _response(200, {"status": "active", "metadata": active_metadata}),
        ]
    )
    client = FoundryDeploymentClient(
        PROJECT_ENDPOINT,
        lambda: "token",
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    receipt = client.deploy_hosted_container(
        agent_name="aiq-005-ticket",
        definition=definition,
        image=image,
        run_id="run-3",
    )
    payload = json.loads(transport.calls[0]["body"])
    assert receipt.artifact_digest == digest
    assert receipt.image_digest == image_digest
    assert payload["definition"]["container_configuration"]["image"] == image
    assert transport.calls[0]["headers"]["Foundry-Features"] == HOSTED_FEATURES
    with pytest.raises(RuntimeContractError, match="public GHCR"):
        validate_image_reference("private.azurecr.io/ticket:latest")
    with pytest.raises(RuntimeContractError, match="public GHCR"):
        validate_image_reference("ghcr.io/ninghu/agent-insights-quality-ticket:latest")


def test_cleanup_deletes_only_the_exact_owned_version() -> None:
    digest = "sha256:" + ("b" * 64)
    receipt = DeploymentReceipt(
        agent_name="aiq-004-travel",
        agent_version="9",
        agent_type="hosted_code",
        artifact_digest=digest,
        run_id="run-clean",
        status="active",
    )
    transport = QueueTransport(
        [
            _response(200, {"metadata": _metadata("run-clean", digest)}),
            _response(204),
        ]
    )
    client = FoundryDeploymentClient(
        PROJECT_ENDPOINT, lambda: "token", transport=transport
    )
    client.cleanup_version(receipt)
    assert transport.calls[-1]["method"] == "DELETE"
    assert "/versions/9?" in transport.calls[-1]["url"]

    mismatch = QueueTransport([_response(200, {"metadata": {}})])
    guarded = FoundryDeploymentClient(
        PROJECT_ENDPOINT, lambda: "token", transport=mismatch
    )
    with pytest.raises(RuntimeContractError, match="ownership"):
        guarded.cleanup_version(receipt)
    assert len(mismatch.calls) == 1


def test_prompt_invocation_binds_exact_version_and_returns_non_trace_receipt() -> None:
    fixture = HealthyFixture(
        id="weather-current-seattle",
        input="weather",
        output_contains="partly cloudy",
        tool_outputs={
            "current_weather": {
                "arguments": {"location_id": "loc-sea"},
                "result": {"condition": "partly cloudy"},
            }
        },
        expected_tool_calls=("current_weather",),
    )
    transport = QueueTransport(
        [
            _response(
                200,
                {
                    "id": "resp-1",
                    "status": "in_progress",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "current_weather",
                            "arguments": '{"location_id":"loc-sea"}',
                        }
                    ],
                },
            ),
            _response(
                200,
                {
                    "id": "resp-2",
                    "status": "completed",
                    "output_text": "The evidence says partly cloudy.",
                },
                headers={"x-ms-request-id": "invocation-prompt-2"},
            ),
        ]
    )
    client = FoundryInvocationClient(
        PROJECT_ENDPOINT, lambda: "runtime-token", transport=transport
    )
    receipt = client.invoke_prompt(
        DeploymentReceipt(
            "aiq-001-weather", "12", "prompt", "sha256:" + ("c" * 64), "run", "active"
        ),
        fixture,
    )
    first = json.loads(transport.calls[0]["body"])
    second = json.loads(transport.calls[1]["body"])
    assert first["agent_reference"]["version"] == "12"
    assert transport.calls[0]["url"].endswith("/openai/v1/responses")
    assert second["previous_response_id"] == "resp-1"
    assert receipt.response_id == "resp-2"
    assert receipt.invocation_id == "invocation-prompt-2"
    assert receipt.called_tools == ("current_weather",)
    assert receipt.trace_id is None
    assert all(
        call["headers"]["Authorization"] == "Bearer runtime-token"
        for call in transport.calls
    )


def test_hosted_invocation_routes_to_endpoint_with_exact_session_binding() -> None:
    fixture = HealthyFixture(
        id="finance-budget",
        input="prepare-budget account=SYN-100 monthly_limit=1200",
        output_contains="No transfer was attempted",
        tool_outputs={},
        expected_tool_calls=(),
    )
    transport = QueueTransport(
        [
            _response(
                201,
                {
                    "agent_session_id": "session-8",
                    "version_indicator": {"agent_version": "8"},
                },
            ),
            _response(
                200,
                {
                    "id": "response-8",
                    "status": "completed",
                    "output_text": "No transfer was attempted.",
                },
                headers={"x-request-id": "invocation-hosted-8"},
            ),
            _response(204),
        ]
    )
    client = FoundryInvocationClient(
        PROJECT_ENDPOINT, lambda: "token", transport=transport
    )
    receipt = client.invoke_hosted(
        DeploymentReceipt(
            "aiq-003-finance",
            "8",
            "hosted_code",
            "sha256:" + ("d" * 64),
            "run",
            "active",
        ),
        fixture,
    )
    assert json.loads(transport.calls[0]["body"])["version_indicator"] == {
        "agent_version": "8"
    }
    assert "/endpoint/protocols/openai/responses?api-version=v1" in transport.calls[1][
        "url"
    ]
    assert receipt.session_id == "session-8"
    assert receipt.response_id == "response-8"
    assert receipt.invocation_id == "invocation-hosted-8"
    assert transport.calls[2]["method"] == "DELETE"
    assert all(
        call["headers"]["Foundry-Features"] == HOSTED_FEATURES
        for call in transport.calls
    )


def test_hosted_session_version_mismatch_is_cleaned_up() -> None:
    fixture = HealthyFixture(
        id="travel-flight-search",
        input="search",
        output_contains="unused",
        tool_outputs={},
        expected_tool_calls=(),
    )
    transport = QueueTransport(
        [
            _response(
                201,
                {
                    "agent_session_id": "wrong-session",
                    "version_indicator": {"agent_version": "previous"},
                },
            ),
            _response(204),
        ]
    )
    client = FoundryInvocationClient(
        PROJECT_ENDPOINT, lambda: "token", transport=transport
    )
    deployment = DeploymentReceipt(
        "aiq-004-travel",
        "current",
        "hosted_code",
        "sha256:" + ("f" * 64),
        "run",
        "active",
    )
    with pytest.raises(RuntimeContractError, match="exact deployed version"):
        client.invoke_hosted(deployment, fixture)
    assert transport.calls[-1]["method"] == "DELETE"
    assert "/sessions/wrong-session?" in transport.calls[-1]["url"]


def test_deployment_poll_fails_on_terminal_state_and_timeout() -> None:
    definition = {"kind": "prompt", "model": "test", "instructions": "healthy", "tools": []}
    failed_transport = QueueTransport(
        [
            _response(201, {"version": "1"}),
            _response(200, {"status": "failed", "error": {"code": "CodeError"}}),
        ]
    )
    failed = FoundryDeploymentClient(
        PROJECT_ENDPOINT,
        lambda: "token",
        transport=failed_transport,
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(RuntimeContractError, match="CodeError"):
        failed.deploy_prompt(
            agent_name="aiq-001-weather", definition=definition, run_id="failed"
        )

    ticks = iter((0.0, 1.0, 2.0))
    timeout_transport = QueueTransport(
        [
            _response(201, {"version": "2"}),
            _response(200, {"status": "creating"}),
        ]
    )
    timed = FoundryDeploymentClient(
        PROJECT_ENDPOINT,
        lambda: "token",
        transport=timeout_transport,
        poll_timeout_seconds=1,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(ticks),
    )
    with pytest.raises(RuntimeContractError, match="before timeout"):
        timed.deploy_prompt(
            agent_name="aiq-001-weather", definition=definition, run_id="timed"
        )


def test_healthy_traffic_concurrency_has_stable_receipt_order() -> None:
    fixtures = load_fixtures(
        ROOT / "agents" / "travel-hosted" / "healthy-traffic.json"
    )
    deployment = DeploymentReceipt(
        "aiq-004-travel",
        "1",
        "hosted_code",
        "sha256:" + ("e" * 64),
        "run",
        "active",
    )

    class StubClient:
        def invoke_hosted(self, receipt, fixture):
            return type("Receipt", (), {"fixture_id": fixture.id})()

    receipts = run_healthy_traffic(StubClient(), deployment, fixtures, max_workers=3)
    assert [receipt.fixture_id for receipt in receipts] == [
        fixture.id for fixture in fixtures
    ]
    with pytest.raises(RuntimeContractError, match="between 1 and 8"):
        run_healthy_traffic(StubClient(), deployment, fixtures, max_workers=0)


def test_version_deployment_uses_version_route_without_create_name() -> None:
    definition = {"kind": "prompt", "model": "test", "instructions": "healthy", "tools": []}
    digest = canonical_json_digest(definition)
    transport = QueueTransport(
        [
            _response(201, {"version": "2"}),
            _response(200, {"status": "active", "metadata": _metadata("run-v2", digest)}),
        ]
    )
    client = FoundryDeploymentClient(
        PROJECT_ENDPOINT,
        lambda: "token",
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    client.deploy_prompt(
        agent_name="aiq-001-weather",
        definition=definition,
        run_id="run-v2",
        create_agent=False,
    )
    assert transport.calls[0]["url"].endswith(
        "/agents/aiq-001-weather/versions?api-version=v1"
    )
    assert "name" not in json.loads(transport.calls[0]["body"])


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://dc.applicationinsights.azure.com/api/projects/fake",
        "https://monitor.azure.com/api/projects/fake",
        "http://sample.services.ai.azure.com/api/projects/quality",
    ],
)
def test_endpoint_adapter_rejects_non_foundry_or_ingestion_routes(endpoint: str) -> None:
    with pytest.raises(RuntimeContractError, match="Foundry project endpoint"):
        FoundryInvocationClient(endpoint, lambda: "token", transport=QueueTransport([]))
