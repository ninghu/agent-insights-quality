from __future__ import annotations

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.util import ROOT
from agent_insights_quality.validation_approved import APPROVED_RECORD_CONTAINER
from agent_insights_quality.validation_blob import (
    APPROVED_RECORD_CONTAINER as BLOB_APPROVED_RECORD_CONTAINER,
)


def test_live_validation_gate_workflows_are_removed() -> None:
    for name in (
        "test-agent-validation.yml",
        "test-agent-validation-review.yml",
        "test-agent-validation-receipt.yml",
        "test-agent-validation-reconciler.yml",
    ):
        assert not (ROOT / ".github" / "workflows" / name).exists()


def test_ordinary_validate_workflow_has_no_live_validation_permissions() -> None:
    text = (
        ROOT / ".github" / "workflows" / "validate.yml"
    ).read_text(encoding="utf-8")
    folded = text.casefold()
    assert "python -m pytest" in text
    assert "python -m ruff check ." in text
    assert 'python -m pip install -e ".[dev,azure]"' in text
    for forbidden in (
        "id-token: write",
        "checks: write",
        "azure/login",
        "run-test-agent-validation",
        "approve-test-agent-validation",
        "test-agent-validation-shadow",
        "test-agent-validation-receipt",
    ):
        assert forbidden not in folded


def test_infrastructure_owns_dedicated_sweden_validation_storage() -> None:
    text = (
        ROOT / "infra" / "modules" / "lab.bicep"
    ).read_text(encoding="utf-8")
    assert (
        "resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' ="
        in text
    )
    assert "var storageName = '${storageAccountPrefix}${uniqueSuffix}'" in text
    assert "aiqartifacts" not in text
    assert "resourceRole: storageResourceRole" in text
    assert "allowSharedKeyAccess: false" in text
    assert "allowBlobPublicAccess: false" in text
    assert "isVersioningEnabled: true" in text
    assert "name: qualityArtifactContainerName" in text
    assert "name: deploymentRegistryContainerName" in text
    assert "name: approvedRecordContainerName" in text
    assert "immutableStorageWithVersioning" in text
    assert "immutabilityPolicies" not in text
    assert "qualityArtifactLifecycle" in text
    assert "prefixMatch: ['${qualityArtifactContainerName}/']" in text
    assert "daysAfterModificationGreaterThan: 90" in text
    azure = (
        ROOT / "src" / "agent_insights_quality" / "azure.py"
    ).read_text(encoding="utf-8")
    assert '"immutability-policy",' in azure
    assert '"create",' in azure
    assert '"lock",' in azure
    assert 'policy.get("state") != "Locked"' in azure
    assert "policy.approved_record_container" in azure
    assert "policy.storage_account_prefix" in azure
    assert "policy.storage_resource_role" in azure
    assert (
        APPROVED_RECORD_CONTAINER
        == BLOB_APPROVED_RECORD_CONTAINER
        == load_automation_policy().approved_record_container
        == "test-agent-validation-approved-records-swedencentral-g30"
    )
    assert "test-agent-validation-lifecycle" not in text
    assert "test-agent-validation-snapshots" not in text
    assert "test-agent-validation-shadow-receipts" not in text
    assert "validationPrincipalId" not in text
    assert "validationReceiptPrincipalId" not in text


def test_generated_report_validation_uses_trusted_base_memory_and_history() -> None:
    text = (
        ROOT / ".github" / "workflows" / "validate-generated-change.yml"
    ).read_text(encoding="utf-8")
    assert "base-insight-engine-improvement.json" in text
    assert "base-insight-engine-improvement.md" in text
    assert "--base-improvement-json" in text
    assert "--base-improvement-markdown" in text
    assert 'git cat-file -e "${BASE_SHA}:${current_snapshot}"' in text
    assert "Current improvement snapshot already exists in trusted base" in text
    assert "git diff --no-renames --name-status" in text
    assert "Existing improvement snapshot changed or was deleted" in text
