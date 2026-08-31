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


def test_hosted_sources_do_not_inject_canonical_output_messages() -> None:
    for agent_name in (
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    ):
        source_files = (ROOT / "agents" / agent_name).glob("**/source/*.py")
        assert all(
            "gen_ai.output.messages"
            not in source.read_text(encoding="utf-8")
            for source in source_files
        )


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
    assert (
        "ApplicationInsightsConnectionString: "
        "applicationInsights.properties.ConnectionString"
    ) in content
    lab = (ROOT / "infra" / "modules" / "lab.bicep").read_text(encoding="utf-8")
    assert "eadc314b-1a2d-4efa-be10-5d325db5065e" in lab
    assert "automationRegistryPush" in lab
    assert "principalType: 'User'" in lab
    assert "name: 'gpt-5.4-mini'" in lab
    assert "name: 'terra-insight-generation'" in lab
    assert "name: 'terra-test-agents'" not in lab
    assert "isVersioningEnabled: true" in lab
    assert "test-agent-validation-approved-records" in lab
    assert lab.count("immutableStorageWithVersioning") == 1
    assert "immutabilityPeriodSinceCreationInDays: 90" in lab
    assert "expire-approved-validation-records-after-worm" in lab
    assert "daysAfterModificationGreaterThan: 91" in lab
    assert "expire-deployment-registry-versions" in lab
    assert "test-agent-validation-lifecycle" not in lab
    assert "test-agent-validation-snapshots" not in lab
    assert "test-agent-validation-receipts" not in lab
    assert "validationReceiptPrincipalId" not in lab
    assert "validationPrincipalId" not in lab
    assert "principalType: 'ServicePrincipal'" not in lab


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
