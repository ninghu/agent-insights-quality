from __future__ import annotations

import re

import yaml
from yaml.constructor import ConstructorError

from agent_insights_quality.util import ROOT


def _workflow(name: str) -> str:
    return (
        ROOT / ".github" / "workflows" / name
    ).read_text(encoding="utf-8")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def test_candidate_workflow_is_bounded_nonauthorizing_shadow_preparation() -> None:
    text = _workflow("test-agent-validation.yml")
    folded = text.casefold()
    assert "workflow_dispatch" in text
    assert "test-agent-validation-account" in text
    assert "cancel-in-progress: false" in text
    assert "id-token: write" in text
    assert "test-agent-validation-shadow" in text
    assert "prepare-test-agent-validation" in text
    assert "generate-test-agent-validation-rules --check" in text
    assert "run-test-agent-validation" in text
    assert "--mode $env:VALIDATION_MODE" in text
    assert "uses: azure/login@v2" in text
    assert "EXPECTED_AZURE_CLIENT_ID" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "checks: write" not in text
    assert "attest-test-agent-validation-review" not in text
    assert "--pr-number ${{" not in text
    assert "--candidate-head-sha ${{" not in text
    assert "non-authorizing" in folded
    assert "required check" in folded
    for forbidden in (
        "run-daily",
        "run-full",
        "publish-adx",
        "agent_insight_monitor",
        "create-promotion-receipt",
    ):
        assert forbidden not in folded


def test_receipt_workflow_uses_protected_default_branch_issuer() -> None:
    text = _workflow("test-agent-validation-receipt.yml")
    assert "environment: test-agent-validation-receipt" in text
    assert "ref: main" in text
    assert "issue-test-agent-validation-merge-receipt" in text
    assert "id-token: write" in text
    assert "checks: write" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "uses: azure/login@v2" in text
    assert "AIQ_VALIDATION_RECEIPT_CLIENT_ID" in text
    assert "AIQ_VALIDATION_RECEIPT_PRINCIPAL_ID" in text
    assert "receipt_file" not in text
    assert "--cycle-id $env:CYCLE_ID" in text
    assert "--final-head-sha $env:FINAL_HEAD_SHA" in text
    assert '--candidate-root (Join-Path $env:GITHUB_WORKSPACE "candidate-source")' in text
    assert "path: candidate-source" in text
    assert "persist-credentials: false" in text


def test_reconciler_is_cleanup_only_on_fifteen_minute_cadence() -> None:
    text = _workflow("test-agent-validation-reconciler.yml")
    assert 'cron: "*/15 * * * *"' in text
    assert "cleanup-only:" in text
    assert "reconcile-test-agent-validation" in text
    assert "ref: main" in text
    assert "uses: azure/login@v2" in text
    assert "EXPECTED_AZURE_CLIENT_ID" in text
    assert "EXPECTED_AZURE_OBJECT_ID" in text
    assert '--holder-workflow-reference "${{' not in text
    for forbidden in (
        "prepare-test-agent-validation",
        "run-daily",
        "run-full",
        "issue-test-agent-validation-receipt",
    ):
        assert forbidden not in text


def test_review_workflow_is_protected_default_branch_attestation_producer() -> None:
    text = _workflow("test-agent-validation-review.yml")
    assert "name: test-agent-validation-review" in text
    assert "environment: test-agent-validation-review" in text
    assert "ref: main" in text
    assert "checks: write" in text
    assert "attest-test-agent-validation-review" in text
    assert "--frozen-head-sha $env:FROZEN_HEAD_SHA" in text
    assert "--findings-digest $env:FINDINGS_DIGEST" in text
    assert "AIQ_VALIDATION_RECEIPT_PRINCIPAL_ID" in text


def test_validation_workflows_have_no_duplicate_yaml_keys() -> None:
    for name in (
        "test-agent-validation.yml",
        "test-agent-validation-receipt.yml",
        "test-agent-validation-reconciler.yml",
        "test-agent-validation-review.yml",
    ):
        value = yaml.load(_workflow(name), Loader=_UniqueKeyLoader)
        assert isinstance(value, dict)


def test_validation_receipt_and_reconciler_have_exact_blob_rbac() -> None:
    text = (ROOT / "infra" / "modules" / "lab.bicep").read_text(
        encoding="utf-8"
    )
    for resource, scope, role in (
        (
            "validationReceiptLifecycleContributor",
            "validationLifecycle",
            "blobContributorRoleId",
        ),
        (
            "validationReceiptSnapshotContributor",
            "validationSnapshots",
            "blobContributorRoleId",
        ),
        (
            "validationMergeReceiptReader",
            "validationReceipts",
            "blobReaderRoleId",
        ),
    ):
        assert re.search(
            rf"resource {resource} .*?\{{.*?scope: {scope}.*?"
            rf"roleDefinitionId: .*?{role}",
            text,
            flags=re.DOTALL,
        )
