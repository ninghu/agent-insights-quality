from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.provisioning import (
    FoundryProvisioner,
    RemoteHttpError,
    _build_and_push_support_image,
)
from agent_insights_quality.util import ContractError, ROOT
from agent_insights_quality.validation_provisioning import (
    FoundryAuthorityDeployer,
    ValidationProjectProvisioner,
    _cycle_image_tag,
    _rate_limits,
    validation_runtime_profile,
)
from agent_insights_quality.validation_policy import load_validation_policy


def _active_hosted_version(
    agent_name: str = "synthetic-agent",
) -> dict:
    return {
        "_status": 200,
        "object": "agent.version",
        "id": "synthetic-version",
        "name": agent_name,
        "version": "1",
        "status": "active",
        "agent_guid": "synthetic-agent-guid",
        "definition": {
            "kind": "hosted",
            "cpu": "1",
            "memory": "2Gi",
            "code_configuration": {
                "runtime": "python_3_13",
                "entry_point": ["python", "-m", "agent"],
                "dependency_resolution": "remote_build",
            },
        },
        "instance_identity": {
            "principal_id": "synthetic-instance-principal",
            "client_id": "synthetic-instance-client",
            "status": "active",
        },
        "blueprint": {
            "principal_id": "synthetic-blueprint-principal",
            "client_id": "synthetic-blueprint-client",
            "status": "active",
        },
        "blueprint_reference": {
            "type": "ManagedAgentIdentityBlueprint",
            "blueprint_id": "synthetic-blueprint-reference",
        },
    }


def _staging_profile() -> RuntimeProfile:
    return RuntimeProfile(
        name="staging",
        project_name="agent-insights-quality-staging",
        project_endpoint="https://example.invalid/staging",
        insights_endpoint="https://example.invalid/staging",
        application_insights_resource_id=(
            "/subscriptions/synthetic/resourceGroups/synthetic/providers/"
            "Microsoft.Insights/components/synthetic-g29"
        ),
        registry_path=Path("registry.json"),
        account_name="synthetic",
        container_registry_name="syntheticregistry",
        registry_storage_account_name="syntheticstorage",
        account_resource_id=(
            "/subscriptions/synthetic/resourceGroups/synthetic/providers/"
            "Microsoft.CognitiveServices/accounts/synthetic"
        ),
        telemetry_resource_set="g29",
    )


def test_validation_profile_reuses_staging_account_but_not_staging_project() -> None:
    profile = validation_runtime_profile(
        "aiq-validation-0123456789ab",
        cycle_id="validation-cycle-0001",
        base=_staging_profile(),
    )
    assert profile.account_name == "synthetic"
    assert profile.telemetry_resource_set == "g29"
    assert profile.project_name == "aiq-validation-0123456789ab"
    assert "agent-insights-quality-staging" not in profile.project_endpoint
    assert "test-agent-validation" in str(profile.registry_path)


def test_validation_profile_has_no_staging_fallback() -> None:
    incomplete = _staging_profile()
    incomplete = RuntimeProfile(
        **{**incomplete.__dict__, "account_name": ""}
    )
    with pytest.raises(ContractError, match="staging Foundry account"):
        validation_runtime_profile(
            "aiq-validation-0123456789ab",
            cycle_id="validation-cycle-0001",
            base=incomplete,
        )


def test_validation_project_bicep_creates_no_monitor_or_insights_run() -> None:
    text = (
        ROOT / "infra" / "modules" / "validation-project.bicep"
    ).read_text(encoding="utf-8")
    assert "Microsoft.CognitiveServices/accounts/projects" in text
    assert "application-insights-validation" in text
    assert "container-registry-validation" in text
    assert "validation-project-rbac.bicep" in text
    assert (
        "ApplicationInsightsConnectionString: "
        "applicationInsights.properties.ConnectionString"
    ) in text
    assert "ownershipNonce" in text
    assert "agent_insight" not in text.casefold()
    assert "monitor" not in text.casefold().replace("monitoringreader", "")


def test_project_children_have_deterministic_intents_before_bicep() -> None:
    provisioner = ValidationProjectProvisioner(
        _staging_profile(),
        local_operator_id="synthetic-local-operator",
        policy=load_validation_policy(),
    )
    intents = provisioner.resource_intents(
        project_name="aiq-validation-0123456789ab",
        cycle_id="validation-cycle-0001",
        ownership_nonce="nonce-0001",
    )
    assert [item["kind"] for item in intents] == [
        "arm_deployment",
        "runtime_principal",
        "connection",
        "connection",
        "role_assignment",
        "role_assignment",
        "role_assignment",
        "role_assignment",
    ]
    assert len({item["intent_reference"] for item in intents}) == len(intents)
    assert all(item["runtime_kind"] == "control" for item in intents)
    assert all(item["discovery_key"] for item in intents)
    bicep = (
        ROOT / "infra" / "modules" / "validation-project.bicep"
    ).read_text(encoding="utf-8")
    assert "validationOperatorProjectManagerName" in bicep
    assert "appInsightsReaderName" in bicep


def test_project_timeout_cancels_and_waits_for_terminal_deployment(
    monkeypatch,
) -> None:
    provisioner = ValidationProjectProvisioner(
        _staging_profile(),
        local_operator_id="synthetic-local-operator",
        policy=load_validation_policy(),
    )
    observed = []
    monkeypatch.setattr(
        "agent_insights_quality.validation_provisioning.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("az", 1)
        ),
    )
    monkeypatch.setattr(
        provisioner,
        "_cancel_and_wait_deployment",
        observed.append,
    )
    with pytest.raises(ContractError, match="did not complete locally"):
        provisioner.create(
            project_name="aiq-validation-0123456789ab",
            cycle_id="validation-cycle-0001",
            ownership_nonce="nonce-0001",
        )
    assert observed == ["test-agent-validation-validation-cycle-0001"]


def test_capacity_measurement_normalizes_provider_rate_windows() -> None:
    assert _rate_limits(
        {
            "properties": {
                "rateLimits": [
                    {
                        "key": "requests",
                        "count": 50,
                        "renewalPeriodInSeconds": 10,
                    },
                    {
                        "key": "tokens",
                        "count": 20000,
                        "renewalPeriodInSeconds": 60,
                    },
                ]
            }
        }
    ) == (300, 20000)
    with pytest.raises(ContractError, match="lacks measured RPM/TPM"):
        _rate_limits({"properties": {"rateLimits": []}})


def test_support_cycle_tags_are_deterministic_and_provider_bounded() -> None:
    assert _cycle_image_tag("validation-0123456789ab", "issue-036") == (
        "validation-validation-0123456789ab-issue-036"
    )
    with pytest.raises(ContractError, match="provider limits"):
        _cycle_image_tag("x" * 129, "issue-036")


def test_support_image_records_acr_intents_before_push(
    monkeypatch,
    tmp_path: Path,
) -> None:
    timeline = []
    digest = "sha256:" + ("a" * 64)
    monkeypatch.setattr(
        "agent_insights_quality.provisioning._existing_acr_image",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.runtime_root",
        lambda: tmp_path,
    )

    def run(arguments, **_kwargs):
        timeline.append(("command", tuple(arguments)))
        return SimpleNamespace(
            returncode=0,
            stdout=f"digest: {digest}" if arguments[:2] == ["docker", "push"] else "",
        )

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.subprocess.run",
        run,
    )
    result = _build_and_push_support_image(
        registry="syntheticregistry",
        root=ROOT / "agents" / "support-ticket-agent",
        logical_version="v0",
        wheelhouse_port=12345,
        record_resource=lambda event: timeline.append(
            ("event", event["state"], event["kind"])
        ),
    )
    push_index = next(
        index
        for index, item in enumerate(timeline)
        if item[0] == "command" and item[1][:2] == ("docker", "push")
    )
    assert ("event", "create_intent", "acr_tag") in timeline[:push_index]
    assert ("event", "create_intent", "acr_manifest") in timeline[:push_index]
    assert ("event", "created", "acr_tag") in timeline[push_index + 1 :]
    assert ("event", "created", "acr_manifest") in timeline[push_index + 1 :]
    assert result.endswith(f"@{digest}")


def test_hosted_canary_readiness_rechecks_active_topology() -> None:
    calls = []
    deployer = object.__new__(FoundryAuthorityDeployer)
    details = _active_hosted_version()
    deployer._client = SimpleNamespace(
        version_details=lambda name, version, *, hosted: (
            calls.append((name, version, hosted)) or details
        )
    )
    authority = SimpleNamespace(runtime_kind="hosted_code")
    deployed = SimpleNamespace(
        runtime_agent_name="synthetic-agent",
        runtime_agent_version="1",
        provider_agent_id="synthetic-agent",
        provider_agent_version_id="synthetic-version",
        hosted_identity_id="synthetic-instance-client",
        hosted_blueprint_id="synthetic-blueprint-reference",
        hosted_deployment_id="synthetic-agent-guid",
        runtime_principal_id="synthetic-instance-principal",
    )
    deployer.assert_ready(authority, deployed)
    assert calls == [("synthetic-agent", "1", True)]
    details["status"] = "pending"
    with pytest.raises(ContractError, match="not active"):
        deployer.assert_ready(authority, deployed)
    details["status"] = "active"
    details["agent_guid"] = "different-agent-guid"
    with pytest.raises(ContractError, match="topology is not ready"):
        deployer.assert_ready(authority, deployed)


def test_resumed_canary_readiness_not_found_fails_without_create_proof() -> None:
    calls = []
    deployer = object.__new__(FoundryAuthorityDeployer)

    def version_details(name, version, *, hosted):
        calls.append((name, version, hosted))
        raise RemoteHttpError(
            404,
            "NotFound",
            "Synthetic missing version",
            "GET /agents/synthetic-agent/versions/1",
        )

    deployer._client = SimpleNamespace(version_details=version_details)
    with pytest.raises(RemoteHttpError):
        deployer.assert_ready(
            SimpleNamespace(
                authority_id="synthetic-agent/v0",
                runtime_kind="prompt",
            ),
            SimpleNamespace(
                runtime_agent_name="synthetic-agent",
                runtime_agent_version="1",
                provider_agent_id="synthetic-agent",
                provider_agent_version_id="synthetic-agent/versions/1",
            ),
        )
    assert calls == [("synthetic-agent", "1", False)]


def test_prompt_deploy_reuses_activation_details_after_temporary_propagation(
    monkeypatch,
) -> None:
    now = [10.0]
    sleeps = []
    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.monotonic",
        lambda: now[0],
    )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(
        "agent_insights_quality.provisioning.time.sleep",
        sleep,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_provisioning.source_content_digest",
        lambda *_args, **_kwargs: "sha256:" + ("a" * 64),
    )
    profile = _staging_profile()
    client = FoundryProvisioner(
        profile,
        token_provider=lambda _: "synthetic-token",
    )
    timeline = []
    list_reads = 0
    detail_reads = 0
    metadata = {}

    def request(method, path, *, hosted, body=None, **_kwargs):
        nonlocal detail_reads, list_reads, metadata
        timeline.append((method, path, hosted))
        if path.endswith("/versions?limit=100"):
            list_reads += 1
            if list_reads == 1:
                return {"_status": 200, "data": []}
            return {
                "_status": 200,
                "data": [
                    {
                        "version": "1",
                        "status": "active",
                        "metadata": metadata,
                    }
                ]
            }
        if method == "GET" and path == "/agents/synthetic-weather":
            return {"_status": 404}
        if method == "POST" and path == "/agents":
            metadata = json.loads(body)["metadata"]
            return {"versions": {"latest": {"version": "1"}}}
        if path == "/agents/synthetic-weather/versions/1":
            detail_reads += 1
            if detail_reads == 1:
                raise RemoteHttpError(
                    404,
                    "NotFound",
                    "Exact version propagation pending",
                    "GET /agents/synthetic-weather/versions/1",
                )
            return {
                "status": "active",
                "agent_id": "synthetic-weather",
                "id": "synthetic-weather/versions/1",
                "metadata": metadata,
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    client._request = request  # type: ignore[method-assign]
    deployer = object.__new__(FoundryAuthorityDeployer)
    deployer._profile = profile
    deployer._agents = {
        "weather-agent": {
            "name": "weather-agent",
            "type": "prompt",
            "baseline_path": "agents/weather-agent/v0",
        }
    }
    deployer._issues = {}
    deployer._project = SimpleNamespace(connection_ids=())
    deployer._support_images = {}
    deployer._client = client
    deployer._readiness_lock = threading.Lock()
    deployer._readiness_proofs = {}
    authority = SimpleNamespace(
        authority_id="weather-agent/v0",
        authority_kind="baseline",
        canonical_agent="weather-agent",
        logical_version="v0",
        runtime_kind="prompt",
        source_content_digest="sha256:" + ("a" * 64),
    )
    deployed = deployer.deploy(
        authority,
        SimpleNamespace(runtime_agent_name="synthetic-weather"),
    )
    deployer.assert_ready(authority, deployed)

    assert deployed.runtime_agent_version == "1"
    assert deployed.provider_agent_version_id == "synthetic-weather/versions/1"
    assert list_reads == 2
    assert detail_reads == 2
    assert sleeps == [5]
    assert timeline[-2:] == [
        ("GET", "/agents/synthetic-weather/versions/1", False),
        ("GET", "/agents/synthetic-weather/versions/1", False),
    ]


@pytest.mark.parametrize(
    (
        "canonical_agent",
        "authority_kind",
        "authority_id",
        "logical_version",
        "runtime_kind",
        "runtime_agent_name",
    ),
    [
        (
            "finance-agent",
            "baseline",
            "finance-agent/v0",
            "v0",
            "hosted_code",
            "finance-agent-baseline-cycle",
        ),
        (
            "finance-agent",
            "issue",
            "issue-013",
            "issue-013",
            "hosted_code",
            "finance-agent-issue-013-cycle",
        ),
        (
            "travel-agent",
            "baseline",
            "travel-agent/v0",
            "v0",
            "hosted_code",
            "travel-agent-baseline-cycle",
        ),
        (
            "travel-agent",
            "issue",
            "issue-021",
            "issue-021",
            "hosted_code",
            "travel-agent-issue-021-cycle",
        ),
        (
            "support-ticket-agent",
            "baseline",
            "support-ticket-agent/v0",
            "v0",
            "hosted_custom_container",
            "support-ticket-agent-baseline-cycle",
        ),
        (
            "support-ticket-agent",
            "issue",
            "issue-028",
            "issue-028",
            "hosted_custom_container",
            "support-ticket-agent-issue-028-cycle",
        ),
    ],
)
def test_hosted_deploy_reads_public_version_topology(
    monkeypatch,
    canonical_agent,
    authority_kind,
    authority_id,
    logical_version,
    runtime_kind,
    runtime_agent_name,
) -> None:
    deployer = object.__new__(FoundryAuthorityDeployer)
    detail_reads = []
    details = _active_hosted_version(runtime_agent_name)
    deployer._agents = {
        canonical_agent: {
            "name": canonical_agent,
            "type": "hosted",
            "baseline_path": f"agents/{canonical_agent}/v0",
        }
    }
    deployer._issues = (
        {}
        if authority_kind == "baseline"
        else {
            authority_id: {
                "implementation": (
                    f"agents/{canonical_agent}/versions/{logical_version}"
                )
            }
        }
    )
    deployer._support_images = {}
    deployer._project = SimpleNamespace(
        connection_ids=("synthetic-connection",),
    )
    deployer._client = SimpleNamespace(
        ensure_version_for_readiness=lambda **_kwargs: ("1", details),
        version_details=lambda *_args, **_kwargs: detail_reads.append(True),
    )
    deployer._readiness_lock = threading.Lock()
    deployer._readiness_proofs = {}
    monkeypatch.setattr(
        "agent_insights_quality.validation_provisioning.build_artifact",
        lambda *_args, **_kwargs: {"kind": "hosted_code"},
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_provisioning.source_content_digest",
        lambda *_args, **_kwargs: "synthetic-digest",
    )
    authority = SimpleNamespace(
        authority_id=authority_id,
        authority_kind=authority_kind,
        canonical_agent=canonical_agent,
        logical_version=logical_version,
        runtime_kind=runtime_kind,
        source_content_digest="synthetic-digest",
    )
    deployed = deployer.deploy(
        authority,
        SimpleNamespace(runtime_agent_name=runtime_agent_name),
    )
    deployer.assert_ready(authority, deployed)
    assert deployed.provider_agent_id == runtime_agent_name
    assert deployed.provider_agent_version_id == "synthetic-version"
    assert deployed.hosted_identity_id == "synthetic-instance-client"
    assert deployed.hosted_blueprint_id == "synthetic-blueprint-reference"
    assert deployed.hosted_deployment_id == "synthetic-agent-guid"
    assert deployed.runtime_principal_id == "synthetic-instance-principal"
    assert detail_reads == []


@pytest.mark.parametrize(
    ("field", "value", "error_path"),
    [
        (
            "instance_identity",
            {
                "principal_id": "synthetic-instance-principal",
                "status": "active",
            },
            "instance_identity.client_id",
        ),
        ("blueprint", [], "blueprint"),
        (
            "blueprint_reference",
            {
                "type": "unexpected",
                "blueprint_id": "synthetic-blueprint-reference",
            },
            "blueprint_reference.type",
        ),
        ("agent_guid", None, "agent_guid"),
    ],
)
def test_hosted_canary_readiness_rejects_malformed_public_topology(
    field,
    value,
    error_path,
) -> None:
    details = _active_hosted_version()
    details[field] = value
    deployer = object.__new__(FoundryAuthorityDeployer)
    deployer._client = SimpleNamespace(
        version_details=lambda *_args, **_kwargs: details,
    )
    with pytest.raises(ContractError, match=error_path):
        deployer.assert_ready(
            SimpleNamespace(runtime_kind="hosted_code"),
            SimpleNamespace(
                runtime_agent_name="synthetic-agent",
                runtime_agent_version="1",
                provider_agent_version_id="synthetic-version",
                hosted_identity_id="synthetic-instance-client",
                hosted_blueprint_id="synthetic-blueprint-reference",
                hosted_deployment_id="synthetic-agent-guid",
                runtime_principal_id="synthetic-instance-principal",
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hosted_identity_id", "different-instance-client"),
        ("hosted_blueprint_id", "different-blueprint-reference"),
        ("hosted_deployment_id", "different-agent-guid"),
        ("runtime_principal_id", "different-instance-principal"),
    ],
)
def test_hosted_canary_readiness_rejects_mismatched_topology(
    field,
    value,
) -> None:
    deployed = {
        "runtime_agent_name": "synthetic-agent",
        "runtime_agent_version": "1",
        "provider_agent_version_id": "synthetic-version",
        "hosted_identity_id": "synthetic-instance-client",
        "hosted_blueprint_id": "synthetic-blueprint-reference",
        "hosted_deployment_id": "synthetic-agent-guid",
        "runtime_principal_id": "synthetic-instance-principal",
    }
    deployed[field] = value
    deployer = object.__new__(FoundryAuthorityDeployer)
    deployer._client = SimpleNamespace(
        version_details=lambda *_args, **_kwargs: _active_hosted_version(),
    )
    with pytest.raises(ContractError, match="topology is not ready"):
        deployer.assert_ready(
            SimpleNamespace(runtime_kind="hosted_code"),
            SimpleNamespace(**deployed),
        )
