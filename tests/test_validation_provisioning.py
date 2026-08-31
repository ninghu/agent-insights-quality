from __future__ import annotations

import json
import subprocess
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
    details = {
        "status": "active",
        "id": "synthetic-version",
        "identity": {"id": "synthetic-identity"},
        "blueprint": {"id": "synthetic-blueprint"},
        "deployment": {"id": "synthetic-deployment"},
    }
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
        hosted_identity_id="synthetic-identity",
        hosted_blueprint_id="synthetic-blueprint",
        hosted_deployment_id="synthetic-deployment",
    )
    deployer.assert_ready(authority, deployed)
    assert calls == [("synthetic-agent", "1", True)]
    details["status"] = "pending"
    with pytest.raises(ContractError, match="not active"):
        deployer.assert_ready(authority, deployed)


def test_prompt_deploy_retries_duplicate_detail_read_after_exact_listing(
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
            if detail_reads == 2:
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
    deployed = deployer.deploy(
        SimpleNamespace(
            authority_id="weather-agent/v0",
            authority_kind="baseline",
            canonical_agent="weather-agent",
            logical_version="v0",
            runtime_kind="prompt",
            source_content_digest="sha256:" + ("a" * 64),
        ),
        SimpleNamespace(runtime_agent_name="synthetic-weather"),
    )

    assert deployed.runtime_agent_version == "1"
    assert deployed.provider_agent_version_id == "synthetic-weather/versions/1"
    assert list_reads == 2
    assert detail_reads == 3
    assert sleeps == [5]
    assert timeline[-3:] == [
        ("GET", "/agents/synthetic-weather/versions/1", False),
        ("GET", "/agents/synthetic-weather/versions/1", False),
        ("GET", "/agents/synthetic-weather/versions/1", False),
    ]
