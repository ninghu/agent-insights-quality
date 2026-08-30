from __future__ import annotations

from pathlib import Path

from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_gate import (
    construct_and_issue_merge_receipt,
)
from agent_insights_quality.validation_issuer import (
    CheckState,
    QueriedGitHubState,
    WorkflowState,
)
from agent_insights_quality.validation_policy import load_trusted_policy

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40
TREE = "c" * 40


def _check(name: str, identifier: int, workflow_path: str) -> CheckState:
    return CheckState(
        name=name,
        check_run_id=identifier,
        check_suite_id=identifier + 100,
        app_id=15368,
        app_slug="github-actions",
        head_sha=HEAD,
        conclusion="success",
        result_digest=f"sha256:{identifier:064x}",
        workflow_id=identifier + 200,
        workflow_path=workflow_path,
        workflow_sha=HEAD,
        completed_at="2026-08-29T12:00:00Z",
    )


def test_protected_merge_path_constructs_receipt_from_clean_lifecycle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    policy, policy_digest = load_trusted_policy()
    review = _check(
        policy["checks"]["comprehensive_review"],
        1,
        policy["workflow"]["review_path"],
    )
    targeted = _check(
        policy["checks"]["targeted_verification"],
        2,
        policy["workflow"]["targeted_path"],
    )
    ci = _check(
        policy["checks"]["continuous_integration"],
        3,
        policy["workflow"]["ci_path"],
    )
    active = {
        "state": "CLEAN",
        "cycle_id": "validation-cycle-0001",
        "repository": policy["repository"],
        "pr_number": 999,
        "epoch": 1,
        "git": {"final_head_sha": HEAD, "final_tree_sha": TREE},
        "scope_freeze": {"head_sha": HEAD},
        "review": {
            "mode": "comprehensive",
            "head_sha": HEAD,
            "check_reference": str(review.check_run_id),
            "findings_digest": review.result_digest,
        },
        "clean_snapshot": {
            "path": "test-agent-validation-snapshots/clean.json",
            "version_id": "clean-version",
        },
        "evidence_reference": {
            "path": "test-agent-validation-snapshots/evidence.json",
            "version_id": "evidence-version",
        },
        "lease": {"lease_id": "lease-id"},
    }

    class Store:
        def read(self, container, name, **_kwargs):
            if container == "test-agent-validation-lifecycle":
                return BlobRecord(container, name, active, "active-etag", "active-version")
            return BlobRecord(
                container,
                name,
                {"kind": name},
                f"{name}-etag",
                f"{name}-version",
            )

    store = Store()
    observed = {}
    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.validation_blob_credential",
        lambda expected: observed.setdefault("credential", expected) or object(),
    )
    def store_factory(_account, *, credential):
        observed["store_credential"] = credential
        return store

    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.AzureValidationBlobStore",
        store_factory,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.validate_lifecycle",
        lambda _value: None,
    )

    queried = QueriedGitHubState(
        pr_head_sha=HEAD,
        pr_tree_sha=TREE,
        default_branch_ref="refs/heads/main",
        policy_commit_sha=HEAD,
        policy_content_digest=policy_digest,
        required_checks=tuple(policy["checks"].values()),
        checks=(review, targeted, ci),
        issuer_workflow=WorkflowState(
            workflow_id=400,
            workflow_path=policy["workflow"]["receipt_path"],
            workflow_sha=HEAD,
            head_sha=HEAD,
            run_id=500,
            run_attempt=1,
        ),
    )

    class Reader:
        def __init__(self, token):
            observed["token"] = token

        def read(self, **_kwargs):
            return queried

    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.GhGitHubStateReader",
        Reader,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.github_actions_oidc_subject",
        lambda _values: "synthetic-subject",
    )

    def build_receipt(**kwargs):
        observed["build"] = kwargs
        return {
            "mode": "merge",
            "authorizes_merge": True,
            "cycle_id": active["cycle_id"],
            "epoch": active["epoch"],
            "receipt_digest": HASH,
        }

    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.build_validation_receipt",
        build_receipt,
    )

    class Issuer:
        def __init__(self, value):
            assert value is store

        def issue_merge(self, receipt, **kwargs):
            observed["issued"] = (receipt, kwargs)
            return BlobRecord(
                "test-agent-validation-receipts",
                "receipts/receipt.json",
                receipt,
                "receipt-etag",
                "receipt-version",
            )

    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.ReceiptIssuer",
        Issuer,
    )

    class Controller:
        def __init__(self, _journal, *, lease_id, active):
            assert lease_id == "lease-id"
            assert active.value is active_value

        def receipt_issued(self, record, *, now):
            observed["handoff"] = (record, now)

    active_value = active
    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.ValidationCycleController",
        Controller,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.publish_required_check",
        lambda receipt, **_kwargs: {
            "head_sha": receipt["receipt_digest"],
        },
    )
    private = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setattr(
        "agent_insights_quality.validation_gate.runtime_root",
        lambda: private,
    )
    output = private / "test-agent-validation" / "merge.json"
    result = construct_and_issue_merge_receipt(
        storage_account="syntheticstorage",
        expected_azure_client_id="client-id",
        cycle_id=active["cycle_id"],
        final_head_sha=HEAD,
        receipt_output=output,
        github_token="token",
        environment={"GITHUB_RUN_ID": "500"},
    )
    assert result["state"] == "RECEIPT_ISSUED"
    assert output.is_file()
    assert observed["build"]["mode"] == "merge"
    assert observed["build"]["review"]["check"]["check_run_id"] == 1
    assert observed["handoff"][0].value["receipt_digest"] == HASH
