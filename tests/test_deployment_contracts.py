from __future__ import annotations

from agent_insights_quality.provisioning import _hosted_definition, _resolve_definition
from agent_insights_quality.util import ROOT


def test_hosted_source_uses_supported_runtime() -> None:
    definition = _hosted_definition(
        {"entrypoint": "python -m source.app"},
        profile_endpoint="https://example.invalid",
    )
    assert definition["code_configuration"]["runtime"] == "python_3_13"


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
    lab = (ROOT / "infra" / "modules" / "lab.bicep").read_text(encoding="utf-8")
    assert "eadc314b-1a2d-4efa-be10-5d325db5065e" in lab
    assert "automationRegistryPush" in lab
    assert "principalType: 'User'" in lab
    assert "name: 'gpt-5.4-mini'" in lab
    assert "name: 'terra-insight-generation'" in lab
    assert "name: 'terra-test-agents'" not in lab
    assert "isVersioningEnabled: true" in lab
    assert "test-agent-validation-lifecycle" in lab
    assert "test-agent-validation-snapshots" in lab
    assert "test-agent-validation-receipts" in lab
    assert lab.count("immutableStorageWithVersioning") == 3
    assert "test-agent-validation-shadow-receipts" in lab
    assert "validationReceiptPrincipalId" in lab
    assert "blobReaderRoleId" in lab
    assert "validationPrincipalId" in lab
    assert "principalType: 'ServicePrincipal'" in lab


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
