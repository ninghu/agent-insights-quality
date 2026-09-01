from __future__ import annotations

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.provisioning import (
    _hosted_definition,
    _resolve_definition,
    build_artifact,
)
from agent_insights_quality.util import ROOT
from agent_insights_quality.validation_provisioning import (
    HOSTED_VALIDATION_OUTPUT_TELEMETRY_ENVIRONMENT,
    _build_validation_artifact,
)


def test_hosted_source_uses_supported_runtime() -> None:
    definition = _hosted_definition(
        {"entrypoint": "python -m source.app"},
        profile_endpoint="https://example.invalid",
    )
    assert definition["code_configuration"]["runtime"] == "python_3_13"


def test_validation_output_capture_reaches_every_hosted_deployment_spec() -> None:
    agents, issues = load_catalogs()
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    hosted_agents = [
        agent for agent in agents["agents"] if agent["type"] != "prompt"
    ]
    support_versions = [
        "v0",
        *next(
            agent["issue_ids"]
            for agent in hosted_agents
            if agent["name"] == "support-ticket-agent"
        ),
    ]
    support_images = {
        version: f"synthetic.invalid/support@sha256:{index:064x}"
        for index, version in enumerate(support_versions, start=1)
    }

    definitions = []
    for agent in hosted_agents:
        for logical_version in ["v0", *agent["issue_ids"]]:
            artifact = _build_validation_artifact(
                agent,
                issue_by_id.get(logical_version),
                support_images=support_images,
            )
            definitions.append(artifact["definition"])

    assert len(definitions) == 27
    assert HOSTED_VALIDATION_OUTPUT_TELEMETRY_ENVIRONMENT == {
        "ENABLE_SENSITIVE_DATA": "true",
        "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED": "true",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
    }
    assert all(
        {
            key: definition["environment_variables"].get(key)
            for key in HOSTED_VALIDATION_OUTPUT_TELEMETRY_ENVIRONMENT
        }
        == HOSTED_VALIDATION_OUTPUT_TELEMETRY_ENVIRONMENT
        for definition in definitions
    )


def test_daily_hosted_specs_do_not_enable_validation_content_capture() -> None:
    agents, _ = load_catalogs()
    for agent in agents["agents"]:
        if agent["type"] != "hosted_code":
            continue
        definition = build_artifact(agent, None)["definition"]
        assert not (
            HOSTED_VALIDATION_OUTPUT_TELEMETRY_ENVIRONMENT.keys()
            & definition["environment_variables"].keys()
        )


def test_provider_instrumented_sources_do_not_inject_output_messages() -> None:
    for agent_name in ("finance-agent", "travel-agent"):
        source_files = (ROOT / "agents" / agent_name).glob("**/source/*.py")
        assert all(
            "gen_ai.output.messages"
            not in source.read_text(encoding="utf-8")
            for source in source_files
        )


def test_finance_authorities_enable_maf_before_runtime_construction() -> None:
    finance_root = ROOT / "agents" / "finance-agent"
    requirements = (finance_root / "v0" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    app_sources = [
        path.read_text(encoding="utf-8")
        for path in sorted(finance_root.glob("**/source/app.py"))
    ]
    observability_sources = [
        path.read_text(encoding="utf-8")
        for path in sorted(finance_root.glob("**/source/observability.py"))
    ]

    assert "agent-framework-core==1.14.0" in requirements
    assert len(app_sources) == 9
    assert len(observability_sources) == 9
    assert all(
        source.count("trace.set_tracer_provider(provider)") == 1
        and "enable_instrumentation" not in source
        for source in observability_sources
    )
    for source in app_sources:
        assert (
            "from agent_framework.observability import enable_instrumentation"
            in source
        )
        assert source.count("enable_instrumentation(") == 1
        main = source[source.index("def main() -> None:") :]
        host_setup = "host = ResponsesHostServer(build_agent())"
        enable_maf = "enable_instrumentation(enable_sensitive_data=enable_sensitive_data)"
        host_run = "host.run(port=port)"
        assert (
            'os.getenv("ENABLE_SENSITIVE_DATA", "").strip().casefold() == "true"'
            in main
        )
        assert "enable_sensitive_data=True" not in source
        assert main.index(enable_maf) < main.index(host_setup) < main.index(host_run)
        assert "configure_otel_providers" not in source
        assert "gen_ai.output.messages" not in source


def test_travel_authorities_use_supported_langgraph_instrumentation() -> None:
    travel_root = ROOT / "agents" / "travel-agent"
    requirements = (travel_root / "v0" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    observability_files = list(travel_root.glob("**/source/observability.py"))
    observability_sources = [
        source.read_text(encoding="utf-8") for source in observability_files
    ]

    assert "langchain-azure-ai[hosting,opentelemetry]==1.2.8" in requirements
    assert len(observability_files) == 9
    assert all(
        "enable_auto_tracing(" in source
        and "trace_all_langgraph_nodes=False" in source
        for source in observability_sources
    )


def test_prompt_model_resolves_to_deployment_name() -> None:
    value = _resolve_definition(
        {"kind": "prompt", "model": "gpt-5.4-mini"},
        project_endpoint="https://example.invalid",
    )
    assert value["model"] == "gpt-5.4-mini"


def test_infrastructure_grants_automation_data_plane_roles() -> None:
    content = (
        ROOT / "infra" / "modules" / "profile-project.bicep"
    ).read_text(encoding="utf-8")
    assert "automationInsightsReader" in content
    assert "automationProjectManager" in content
    assert "@allowed(['daily', 'staging'])" in content
    assert (
        "Microsoft.CognitiveServices/accounts/connections@2025-06-01"
        not in content
    )
    assert (
        "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
        in content
    )
    assert content.count("name: 'application-insights-${profile}'") == 1
    assert "isSharedToAll: true" not in content
    assert (
        content.count("key: applicationInsights.properties.ConnectionString")
        == 1
    )
    assert content.count("ApiType: 'Azure'") == 1
    assert content.count("ResourceId: applicationInsights.id") == 1
    assert "purpose: 'agent-insights-quality'" in content
    assert "profile: profile" in content
    assert "ApplicationInsightsConnectionString" not in content
    lab = (ROOT / "infra" / "modules" / "lab.bicep").read_text(encoding="utf-8")
    assert "eadc314b-1a2d-4efa-be10-5d325db5065e" in lab
    assert "automationRegistryPush" in lab
    assert "principalType: 'User'" in lab
    assert "name: 'gpt-5.4-mini'" in lab
    assert "name: 'terra-insight-generation'" in lab
    assert "name: 'terra-test-agents'" not in lab
    assert (
        "resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' "
        "existing"
    ) in lab
    assert (
        "resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' ="
        in lab
    )
    assert "var storageName = '${storageAccountPrefix}${uniqueSuffix}'" in lab
    assert "substring(uniqueString(subscription().subscriptionId, resourceGroup().id), 0, 11)" in lab
    assert len("aiqsweart") + 11 <= 24
    assert "aiqartifacts" not in lab
    assert "resourceRole: storageResourceRole" in lab
    assert "kind: 'StorageV2'" in lab
    assert "name: 'Standard_LRS'" in lab
    assert "allowBlobPublicAccess: false" in lab
    assert "allowSharedKeyAccess: false" in lab
    assert "minimumTlsVersion: 'TLS1_2'" in lab
    assert "supportsHttpsTrafficOnly: true" in lab
    assert (
        "resource blobService "
        "'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' ="
        in lab
    )
    assert "isVersioningEnabled: true" in lab
    assert "name: qualityArtifactContainerName" in lab
    assert "name: deploymentRegistryContainerName" in lab
    assert "name: approvedRecordContainerName" in lab
    assert "immutableStorageWithVersioning" in lab
    assert "immutabilityPolicies" not in lab
    assert "approvedValidationRecordPolicy" not in lab
    assert "qualityArtifactLifecycle" in lab
    assert "Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01" in lab
    assert "prefixMatch: ['${qualityArtifactContainerName}/']" in lab
    assert "daysAfterModificationGreaterThan: 90" in lab
    assert "scope: storage" in lab
    assert "test-agent-validation-lifecycle" not in lab
    assert "test-agent-validation-snapshots" not in lab
    assert "test-agent-validation-receipts" not in lab
    assert "validationReceiptPrincipalId" not in lab
    assert "validationPrincipalId" not in lab
    assert lab.count("scope: storage") == 1


def test_support_provisioning_publishes_to_acr() -> None:
    content = (
        ROOT / "src" / "agent_insights_quality" / "provisioning.py"
    ).read_text(encoding="utf-8")
    assert '"acr", "login"' in content
    assert ".azurecr.io/agent-insights-quality-support" in content
    assert '"docker", "push"' in content


def test_generated_workflow_checks_rename_sources() -> None:
    content = (
        ROOT / ".github" / "workflows" / "validate-generated-change.yml"
    ).read_text(encoding="utf-8")
    assert "--name-status -z --find-renames" in content
    assert 'args+=("--path=${first_path}")' in content
    assert 'args+=("--path=${second_path}")' in content
