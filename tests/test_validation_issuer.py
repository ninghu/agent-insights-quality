from __future__ import annotations

from copy import deepcopy
import json
import runpy
from typing import Any

import pytest

from agent_insights_quality.util import ROOT, ContractError, content_hash
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_issuer import (
    CheckState,
    current_issuer_code_digest,
    QueriedGitHubState,
    ReceiptIssuer,
    WorkflowState,
    oidc_subject_digest,
    publish_required_check,
    verify_runtime_issuer,
    stamp_receipt_digest,
    validate_receipt,
)
from agent_insights_quality.validation_policy import load_trusted_policy

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40
TREE = "c" * 40
NOW = "2026-08-29T12:00:00Z"
OIDC_SUBJECT = (
    "repo:ninghu/agent-insights-quality:"
    "environment:test-agent-validation-receipt"
)


def _check(name: str, identifier: int) -> dict:
    return {
        "name": name,
        "check_run_id": identifier,
        "check_suite_id": identifier + 100,
        "app_id": 15368,
        "app_slug": "github-actions",
        "workflow_id": identifier + 200,
        "workflow_path": ".github/workflows/test-agent-validation-receipt.yml",
        "workflow_sha": HEAD,
        "head_sha": HEAD,
        "conclusion": "success",
        "completed_at": NOW,
        "result_digest": content_hash({"check": identifier}),
    }


def _authorities() -> list[dict]:
    agents = [
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    ]
    values = []
    for agent in agents:
        values.append(
            {
                "authority_id": f"{agent}/v0",
                "source_content_digest": HASH,
                "execution_digest": HASH,
                "pass": True,
                "scenarios": [
                    {
                        "scenario_id": "reviewed-path",
                        "validation_mode": "baseline",
                        "n": 5,
                        "k": 5,
                        "complete_count": 5,
                        "observed": 0,
                        "v0_complete_count": 0,
                        "v0_observed": 0,
                        "pass": True,
                    }
                ],
                "authority_evidence_digest": HASH,
            }
        )
    for number in range(1, 37):
        mode = (
            "model_mediated"
            if number <= 12 or number in {21, 25, 26}
            else "deterministic"
        )
        n = 7 if mode == "model_mediated" else 5
        values.append(
            {
                "authority_id": f"issue-{number:03d}",
                "source_content_digest": HASH,
                "execution_digest": HASH,
                "pass": True,
                "scenarios": [
                    {
                        "scenario_id": "reviewed-path",
                        "validation_mode": mode,
                        "n": n,
                        "k": 5,
                        "complete_count": n,
                        "observed": 5,
                        "v0_complete_count": n,
                        "v0_observed": 0,
                        "pass": True,
                    }
                ],
                "authority_evidence_digest": HASH,
            }
        )
    return values


def _receipt(mode: str) -> dict:
    merge = mode == "merge"
    review_check = _check("test-agent-validation-review", 1) if merge else None
    if review_check is not None:
        review_check["workflow_path"] = (
            ".github/workflows/test-agent-validation.yml"
        )
    policy, policy_digest = load_trusted_policy()
    evidence = runpy.run_path(
        str(ROOT / "tests" / "test_validation_evidence.py")
    )["_evidence"]()
    authority_summaries = [
        {
            "authority_id": authority["authority_id"],
            "source_content_digest": authority["source_content_digest"],
            "execution_digest": authority["execution_digest"],
            "pass": authority["pass"],
            "scenarios": [
                {
                    "scenario_id": scenario["scenario_id"],
                    "validation_mode": scenario["validation_mode"],
                    "n": scenario["n"],
                    "k": scenario["k"],
                    "complete_count": scenario["complete_count"],
                    "observed": scenario["observed"],
                    "v0_complete_count": sum(
                        item["complete"] is True
                        for item in scenario["v0_attempts"]
                    ),
                    "v0_observed": sum(
                        item["defect_observed"] is True
                        for item in scenario["v0_attempts"]
                    ),
                    "pass": scenario["pass"],
                }
                for scenario in authority["scenarios"]
            ],
            "authority_evidence_digest": authority[
                "authority_evidence_digest"
            ],
        }
        for authority in evidence["authorities"]
    ]
    return _with_expected_check_paths(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-receipt",
            "mode": mode,
            "authorizes_merge": merge,
            "repository": "ninghu/agent-insights-quality",
            "pr_number": 999,
            "cycle_id": "validation-cycle-0001",
            "epoch": 1,
            "final_head_sha": HEAD,
            "final_tree_sha": TREE,
            "issued_at": NOW,
            "issuer": {
                "environment": (
                    "test-agent-validation-receipt"
                    if merge
                    else "test-agent-validation-shadow"
                ),
                "oidc_subject_digest": (
                    oidc_subject_digest(OIDC_SUBJECT) if merge else HASH
                ),
                "app_id": 15368,
                "app_slug": "github-actions",
                "workflow_database_id": 300,
                "workflow_path": (
                    ".github/workflows/test-agent-validation-receipt.yml"
                    if merge
                    else ".github/workflows/test-agent-validation.yml"
                ),
                "workflow_ref": "refs/heads/main" if merge else HEAD,
                "workflow_commit_sha": HEAD,
                "run_id": 400,
                "run_attempt": 1,
                "issuer_code_digest": current_issuer_code_digest(),
            },
            "trusted_policy": {
                "default_branch_trust_anchor_present": merge,
                "repository": "ninghu/agent-insights-quality",
                "path": "config/test-agent-validation-policy.yaml",
                "ref": "refs/heads/main" if merge else HEAD,
                "commit_sha": HEAD,
                "content_digest": policy_digest,
                "allowed_workflow_path": (
                    ".github/workflows/test-agent-validation-receipt.yml"
                    if merge
                    else ".github/workflows/test-agent-validation.yml"
                ),
                "allowed_app_id": 15368,
                "allowed_app_slug": "github-actions",
                "required_environment": "test-agent-validation-receipt",
                "required_check": "test-agent-validation",
            },
            "scope_freeze": {
                "head_sha": HEAD,
                "tree_sha": TREE,
                "frozen_at": NOW,
                "source_tree_digest": HASH,
                "validation_contract_digest": HASH,
                "scope_digest": HASH,
            },
            "review": {
                "status": "success" if merge else "skipped",
                "comprehensive_review_count": 1 if merge else 0,
                "exercised_requirements": (
                    ["comprehensive_review"] if merge else ["lifecycle"]
                ),
                "missing_requirements": [] if merge else ["comprehensive_review"],
                "check": review_check,
                "findings_digest": HASH if merge else None,
            },
            "required_ci": {
                "targeted_verification": _check(
                    "test-agent-validation-targeted",
                    2,
                ),
                "continuous_integration": _check("validate", 3),
            },
            "catalog_hashes": {
                "agents": HASH,
                "issues": HASH,
                "artifacts": HASH,
            },
            "artifact_manifest_hash": HASH,
            "source_tree_digest": HASH,
            "validation_contract_digest": HASH,
            "execution_matrix_digest": HASH,
            "runtime_topology_digest": HASH,
            "quota_plan_digest": HASH,
            "evidence_digest": evidence["evidence_digest"],
            "telemetry_resource_set": "g29",
            "test_agent_model": {
                "deployment_name": "gpt-5.4-mini",
                "model_id": "gpt-5.4-mini",
                "model_version": "2026-03-17",
            },
            "authorities": authority_summaries,
            "clean_snapshot": {
                "path": "test-agent-validation-snapshots/clean/final.json",
                "version_id": "version-1",
                "etag": "etag-1",
                "digest": HASH,
                "cleanup_plan_hash": HASH,
                "exact_clean": True,
                "verified_at": NOW,
            },
            "receipt_digest": HASH,
        }
    )


def _with_expected_check_paths(receipt: dict) -> dict:
    if receipt["review"]["check"] is not None:
        receipt["review"]["check"]["workflow_path"] = (
            ".github/workflows/test-agent-validation.yml"
        )
    receipt["required_ci"]["targeted_verification"]["workflow_path"] = (
        ".github/workflows/test-agent-validation.yml"
    )
    receipt["required_ci"]["continuous_integration"]["workflow_path"] = (
        ".github/workflows/validate.yml"
    )
    return stamp_receipt_digest(receipt)


class MemoryReceiptStore:
    def __init__(self, receipt: dict) -> None:
        self.values: dict[tuple[str, str], BlobRecord] = {}
        self.receipt = receipt
        self.evidence = runpy.run_path(
            str(ROOT / "tests" / "test_validation_evidence.py")
        )["_evidence"]()
        assert self.evidence["evidence_digest"] == receipt["evidence_digest"]

    def read(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
        version_id: str | None = None,
    ) -> BlobRecord:
        del lease_id
        clean = self.receipt["clean_snapshot"]
        if (
            container == "test-agent-validation-snapshots"
            and name.startswith("clean/")
        ):
            assert version_id == clean["version_id"]
            return BlobRecord(
                container,
                name,
                {
                    "snapshot_type": "clean",
                    "state": "CLEAN",
                    "cycle_id": self.receipt["cycle_id"],
                    "epoch": self.receipt["epoch"],
                    "journal_digest": clean["digest"],
                    "cleanup": {
                        "plan_hash": clean["cleanup_plan_hash"],
                        "exact_clean": True,
                        "residue_ids": [],
                    },
                    "digests": {
                        "evidence_digest": self.receipt["evidence_digest"]
                    },
                    "evidence_reference": {
                        "path": (
                            "test-agent-validation-snapshots/"
                            "evidence/evidence.json"
                        ),
                        "version_id": "evidence-version",
                        "etag": "evidence-etag",
                        "digest": self.evidence["evidence_digest"],
                    },
                    "git": {
                        "final_head_sha": self.receipt["final_head_sha"],
                        "final_tree_sha": self.receipt["final_tree_sha"],
                    },
                },
                clean["etag"],
                clean["version_id"],
            )
        if container == "test-agent-validation-snapshots":
            assert name == "evidence/evidence.json"
            assert version_id == "evidence-version"
            return BlobRecord(
                container,
                name,
                deepcopy(self.evidence),
                "evidence-etag",
                "evidence-version",
            )
        assert (container, name) == (
            "test-agent-validation-lifecycle",
            "active.json",
        )
        return BlobRecord(
            container,
            name,
            {
                "state": "CLEAN",
                "cycle_id": self.receipt["cycle_id"],
                "epoch": self.receipt["epoch"],
                "clean_snapshot": {
                    "path": clean["path"],
                    "version_id": clean["version_id"],
                    "etag": clean["etag"],
                    "digest": clean["digest"],
                },
            },
            "active-etag",
            "active-version",
        )

    def create_once(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
    ) -> BlobRecord:
        key = (container, name)
        if key in self.values and self.values[key].value != value:
            raise ContractError("different content")
        record = self.values.setdefault(
            key,
            BlobRecord(container, name, deepcopy(value), "etag-1", "version-1"),
        )
        return deepcopy(record)


class Reader:
    def __init__(self, receipt: dict) -> None:
        self.receipt = receipt

    def read(self, **kwargs) -> QueriedGitHubState:
        del kwargs
        checks = [
            self.receipt["review"]["check"],
            self.receipt["required_ci"]["targeted_verification"],
            self.receipt["required_ci"]["continuous_integration"],
        ]
        return QueriedGitHubState(
            pr_head_sha=HEAD,
            pr_tree_sha=TREE,
            default_branch_ref="refs/heads/main",
            policy_commit_sha=HEAD,
            policy_content_digest=self.receipt["trusted_policy"][
                "content_digest"
            ],
            required_checks=(
                "test-agent-validation-review",
                "test-agent-validation-targeted",
                "validate",
                "test-agent-validation",
            ),
            checks=tuple(
                CheckState(
                    name=item["name"],
                    check_run_id=item["check_run_id"],
                    check_suite_id=item["check_suite_id"],
                    app_id=item["app_id"],
                    app_slug=item["app_slug"],
                    head_sha=item["head_sha"],
                    conclusion=item["conclusion"],
                    result_digest=item["result_digest"],
                    workflow_id=item["workflow_id"],
                    workflow_path=item["workflow_path"],
                    workflow_sha=item["workflow_sha"],
                    completed_at=item["completed_at"],
                )
                for item in checks
            ),
            issuer_workflow=WorkflowState(
                workflow_id=300,
                workflow_path=".github/workflows/test-agent-validation-receipt.yml",
                workflow_sha=HEAD,
                head_sha=HEAD,
                run_id=400,
                run_attempt=1,
            ),
        )


def test_shadow_receipt_is_structurally_complete_but_never_authorizes(tmp_path) -> None:
    receipt = _receipt("shadow")
    validate_receipt(receipt)
    record = ReceiptIssuer(
        MemoryReceiptStore(receipt),
        mirror_root=tmp_path,
    ).issue_shadow(receipt)
    assert record.name.startswith("shadow-receipts/")
    assert record.container == "test-agent-validation-shadow-receipts"
    assert receipt["authorizes_merge"] is False
    assert receipt["trusted_policy"]["default_branch_trust_anchor_present"] is False


def test_merge_receipt_requires_live_default_branch_policy_and_checks(tmp_path) -> None:
    receipt = _receipt("merge")
    policy, _ = load_trusted_policy()
    record = ReceiptIssuer(
        MemoryReceiptStore(receipt),
        mirror_root=tmp_path,
    ).issue_merge(
        receipt,
        trusted_policy=policy,
        reader=Reader(receipt),
        oidc_subject=OIDC_SUBJECT,
        environment={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "ninghu/agent-insights-quality",
            "GITHUB_WORKFLOW_REF": (
                "ninghu/agent-insights-quality/"
                ".github/workflows/test-agent-validation-receipt.yml@"
                "refs/heads/main"
            ),
            "GITHUB_SHA": HEAD,
            "GITHUB_RUN_ID": "400",
            "GITHUB_RUN_ATTEMPT": "1",
        },
    )
    assert record.name.startswith("receipts/")
    assert record.container == "test-agent-validation-receipts"


def test_shadow_cannot_use_protected_path_or_claim_authorization(tmp_path) -> None:
    receipt = _receipt("shadow")
    receipt["authorizes_merge"] = True
    receipt = stamp_receipt_digest(receipt)
    with pytest.raises(ContractError, match="schema error"):
        ReceiptIssuer(
            MemoryReceiptStore(receipt),
            mirror_root=tmp_path,
        ).issue_shadow(receipt)


def test_shadow_receipt_requires_exact_candidate_head_policy(tmp_path) -> None:
    receipt = _receipt("shadow")
    receipt["trusted_policy"]["ref"] = "d" * 40
    receipt = stamp_receipt_digest(receipt)
    with pytest.raises(ContractError, match="candidate-head provenance"):
        ReceiptIssuer(
            MemoryReceiptStore(receipt),
            mirror_root=tmp_path,
        ).issue_shadow(receipt)


def test_review_binds_frozen_scope_while_targeted_checks_bind_final_head() -> None:
    receipt = _receipt("merge")
    final_head = "d" * 40
    receipt["final_head_sha"] = final_head
    receipt["final_tree_sha"] = "e" * 40
    receipt["required_ci"]["targeted_verification"]["head_sha"] = final_head
    receipt["required_ci"]["continuous_integration"]["head_sha"] = final_head
    receipt = stamp_receipt_digest(receipt)
    validate_receipt(receipt)


def test_protected_runtime_identity_rejects_wrong_environment_subject() -> None:
    receipt = _receipt("merge")
    with pytest.raises(ContractError, match="runtime issuer identity"):
        verify_runtime_issuer(
            receipt,
            oidc_subject=(
                "repo:ninghu/agent-insights-quality:"
                "environment:test-agent-validation-shadow"
            ),
            environment={
                "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": "ninghu/agent-insights-quality",
                "GITHUB_WORKFLOW_REF": (
                    "ninghu/agent-insights-quality/"
                    ".github/workflows/test-agent-validation-receipt.yml@"
                    "refs/heads/main"
                ),
                "GITHUB_SHA": HEAD,
                "GITHUB_RUN_ID": "400",
                "GITHUB_RUN_ATTEMPT": "1",
            },
        )


def test_required_check_is_published_on_exact_candidate_head(
    monkeypatch,
) -> None:
    receipt = _receipt("merge")
    policy, _ = load_trusted_policy()
    observed = {}

    def run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["payload"] = json.loads(kwargs["input"])
        observed["token"] = kwargs["env"]["GH_TOKEN"]
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "id": 999,
                        "name": "test-agent-validation",
                        "head_sha": HEAD,
                        "conclusion": "success",
                        "app": {"id": 15368, "slug": "github-actions"},
                    }
                ),
            },
        )()

    monkeypatch.setattr(
        "agent_insights_quality.validation_issuer.subprocess.run",
        run,
    )
    result = publish_required_check(
        receipt,
        trusted_policy=policy,
        token="synthetic-scoped-token",
    )
    assert observed["payload"]["head_sha"] == HEAD
    assert observed["payload"]["external_id"] == receipt["receipt_digest"]
    assert observed["token"] == "synthetic-scoped-token"
    assert "main" not in observed["payload"]["head_sha"]
    assert result["check_run_id"] == 999


def test_merge_receipt_fails_when_head_or_check_provenance_changes(tmp_path) -> None:
    receipt = _receipt("merge")
    policy, _ = load_trusted_policy()
    reader = Reader(receipt)
    state = reader.read()
    reader.read = lambda **kwargs: QueriedGitHubState(
        **{**state.__dict__, "pr_head_sha": "d" * 40}
    )
    with pytest.raises(ContractError, match="head changed"):
        ReceiptIssuer(
            MemoryReceiptStore(receipt),
            mirror_root=tmp_path,
        ).issue_merge(
            receipt,
            trusted_policy=policy,
            reader=reader,
            oidc_subject=OIDC_SUBJECT,
            environment={
                "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": "ninghu/agent-insights-quality",
                "GITHUB_WORKFLOW_REF": (
                    "ninghu/agent-insights-quality/"
                    ".github/workflows/test-agent-validation-receipt.yml@"
                    "refs/heads/main"
                ),
                "GITHUB_SHA": HEAD,
                "GITHUB_RUN_ID": "400",
                "GITHUB_RUN_ATTEMPT": "1",
            },
        )


def test_receipt_summaries_must_match_immutable_evidence(tmp_path) -> None:
    receipt = _receipt("shadow")
    receipt["authorities"][0]["execution_digest"] = content_hash("tampered")
    receipt = stamp_receipt_digest(receipt)
    with pytest.raises(ContractError, match="immutable evidence"):
        ReceiptIssuer(
            MemoryReceiptStore(receipt),
            mirror_root=tmp_path,
        ).issue_shadow(receipt)


def test_create_once_receipt_retry_rejects_different_digest(tmp_path) -> None:
    receipt = _receipt("shadow")
    store = MemoryReceiptStore(receipt)
    issuer = ReceiptIssuer(store, mirror_root=tmp_path)
    issuer.issue_shadow(receipt)
    changed = deepcopy(receipt)
    changed["issued_at"] = "2026-08-29T12:01:00Z"
    changed = stamp_receipt_digest(changed)
    with pytest.raises(ContractError, match="different content"):
        issuer.issue_shadow(changed)
