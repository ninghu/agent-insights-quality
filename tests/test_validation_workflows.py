from __future__ import annotations

from agent_insights_quality.util import ROOT


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


def test_infrastructure_has_only_approved_validation_blob_container() -> None:
    text = (
        ROOT / "infra" / "modules" / "lab.bicep"
    ).read_text(encoding="utf-8")
    assert "test-agent-validation-approved-records" in text
    assert "immutableStorageWithVersioning" in text
    assert "immutabilityPeriodSinceCreationInDays: 90" in text
    assert "expire-approved-validation-records-after-worm" in text
    assert "daysAfterModificationGreaterThan: 91" in text
    assert "expire-deployment-registry-versions" in text
    azure = (
        ROOT / "src" / "agent_insights_quality" / "azure.py"
    ).read_text(encoding="utf-8")
    assert '"immutability-policy",' in azure
    assert '"lock",' in azure
    assert 'policy.get("state") != "Locked"' in azure
    assert "test-agent-validation-lifecycle" not in text
    assert "test-agent-validation-snapshots" not in text
    assert "test-agent-validation-shadow-receipts" not in text
    assert "validationPrincipalId" not in text
    assert "validationReceiptPrincipalId" not in text
