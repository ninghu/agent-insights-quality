from __future__ import annotations

import base64
import copy
import json
import subprocess
import os
import urllib.parse
import urllib.request
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    content_hash,
    file_hash,
    immutable_json,
    read_yaml,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_evidence import (
    EXPECTED_BASELINE_AUTHORITIES,
    EXPECTED_ISSUE_AUTHORITIES,
    validate_evidence,
)

RECEIPT_SCHEMA = ROOT / "schemas" / "test-agent-validation-receipt.schema.json"
TRUSTED_POLICY_PATH = ROOT / "config" / "test-agent-validation-policy.yaml"
MERGE_RECEIPT_CONTAINER = "test-agent-validation-receipts"
SHADOW_RECEIPT_CONTAINER = "test-agent-validation-shadow-receipts"


@dataclass(frozen=True)
class CheckState:
    name: str
    check_run_id: int
    check_suite_id: int
    app_id: int
    app_slug: str
    head_sha: str
    conclusion: str
    result_digest: str
    workflow_id: int
    workflow_path: str
    workflow_sha: str
    completed_at: str


@dataclass(frozen=True)
class WorkflowState:
    workflow_id: int
    workflow_path: str
    workflow_sha: str
    head_sha: str
    run_id: int
    run_attempt: int


@dataclass(frozen=True)
class QueriedGitHubState:
    pr_head_sha: str
    pr_tree_sha: str
    default_branch_ref: str
    policy_commit_sha: str
    policy_content_digest: str
    required_checks: tuple[str, ...]
    checks: tuple[CheckState, ...]
    issuer_workflow: WorkflowState


class GitHubStateReader(Protocol):
    def read(
        self,
        *,
        repository: str,
        pr_number: int,
        final_head_sha: str,
        policy_path: str,
        default_branch: str,
        issuer_run_id: int,
        review_head_sha: str,
    ) -> QueriedGitHubState: ...


class ReceiptBlobStore(Protocol):
    def read(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
        version_id: str | None = None,
    ) -> BlobRecord: ...

    def create_once(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
    ) -> BlobRecord: ...


def receipt_digest(value: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop("receipt_digest", None)
    return content_hash(payload)


def stamp_receipt_digest(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["receipt_digest"] = receipt_digest(result)
    return result


def oidc_subject_digest(subject: str) -> str:
    if not subject:
        raise ContractError("GitHub OIDC subject is missing")
    return content_hash({"subject": subject})


def current_issuer_code_digest() -> str:
    paths = (
        ROOT / "src" / "agent_insights_quality" / "validation_issuer.py",
        ROOT / "src" / "agent_insights_quality" / "validation_blob.py",
        ROOT / "src" / "agent_insights_quality" / "validation_evidence.py",
        ROOT / "src" / "agent_insights_quality" / "validation_policy.py",
        ROOT / "src" / "agent_insights_quality" / "util.py",
        ROOT / "schemas" / "test-agent-validation-receipt.schema.json",
        ROOT / "schemas" / "test-agent-validation-evidence.schema.json",
        ROOT / "schemas" / "test-agent-validation-lifecycle.schema.json",
        ROOT / "schemas" / "test-agent-validation-policy.schema.json",
        ROOT / "config" / "test-agent-validation-policy.yaml",
        ROOT / ".github" / "workflows" / "test-agent-validation-receipt.yml",
    )
    return content_hash(
        {
            path.relative_to(ROOT).as_posix(): file_hash(path)
            for path in paths
        }
    )


def github_actions_oidc_subject(
    environment: Mapping[str, str] | None = None,
) -> str:
    values = environment or os.environ
    request_url = str(values.get("ACTIONS_ID_TOKEN_REQUEST_URL") or "")
    request_token = str(values.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN") or "")
    if not request_url or not request_token:
        raise ContractError("Protected GitHub OIDC request context is missing")
    separator = "&" if "?" in request_url else "?"
    url = (
        request_url
        + separator
        + urllib.parse.urlencode({"audience": "api://AzureADTokenExchange"})
    )
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {request_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError("Protected GitHub OIDC token request failed") from error
    token = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(token, str):
        raise ContractError("Protected GitHub OIDC token is missing")
    parts = token.split(".")
    if len(parts) != 3:
        raise ContractError("Protected GitHub OIDC token is malformed")
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(parts[1] + ("=" * (-len(parts[1]) % 4)))
        )
    except (ValueError, json.JSONDecodeError) as error:
        raise ContractError("Protected GitHub OIDC claims are invalid") from error
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not subject:
        raise ContractError("Protected GitHub OIDC subject is missing")
    return subject


def verify_runtime_issuer(
    receipt: Mapping[str, Any],
    *,
    oidc_subject: str,
    environment: Mapping[str, str],
) -> None:
    issuer = receipt["issuer"]
    expected_subject = (
        f"repo:{receipt['repository']}:"
        f"environment:{issuer['environment']}"
    )
    expected_workflow_ref = (
        f"{receipt['repository']}/{issuer['workflow_path']}@"
        f"{issuer['workflow_ref']}"
    )
    if (
        oidc_subject != expected_subject
        or issuer["oidc_subject_digest"] != oidc_subject_digest(oidc_subject)
        or environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("GITHUB_REPOSITORY") != receipt["repository"]
        or environment.get("GITHUB_WORKFLOW_REF") != expected_workflow_ref
        or environment.get("GITHUB_SHA") != issuer["workflow_commit_sha"]
        or str(environment.get("GITHUB_RUN_ID") or "") != str(issuer["run_id"])
        or str(environment.get("GITHUB_RUN_ATTEMPT") or "")
        != str(issuer["run_attempt"])
        or issuer["issuer_code_digest"] != current_issuer_code_digest()
    ):
        raise ContractError("Protected runtime issuer identity is invalid")


def validate_receipt(value: Mapping[str, Any]) -> None:
    schema = read_json(RECEIPT_SCHEMA)
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Test Agent validation receipt schema error at {location}: "
            f"{error.message}"
        )
    if value["receipt_digest"] != receipt_digest(value):
        raise ContractError("Test Agent validation receipt digest is stale")
    authorities = value["authorities"]
    authority_ids = [item["authority_id"] for item in authorities]
    if set(authority_ids) != EXPECTED_BASELINE_AUTHORITIES | EXPECTED_ISSUE_AUTHORITIES:
        raise ContractError("Receipt must summarize the exact 41 authorities")
    if len(authority_ids) != len(set(authority_ids)):
        raise ContractError("Receipt authority IDs must be unique")
    final_head = value["final_head_sha"]
    if (
        value["required_ci"]["targeted_verification"]["head_sha"] != final_head
        or value["required_ci"]["continuous_integration"]["head_sha"] != final_head
    ):
        raise ContractError("Receipt checks are not bound to the exact final head")
    review_check = value["review"]["check"]
    if (
        review_check is not None
        and review_check["head_sha"] != value["scope_freeze"]["head_sha"]
    ):
        raise ContractError("Receipt review is not bound to the frozen scope")
    for authority in authorities:
        for scenario in authority["scenarios"]:
            mode = scenario["validation_mode"]
            if authority["authority_id"].endswith("/v0"):
                expected_mode = "baseline"
            else:
                number = int(
                    authority["authority_id"].removeprefix("issue-")
                )
                expected_mode = (
                    "model_mediated"
                    if number <= 12 or number in {21, 25, 26}
                    else "deterministic"
                )
            if mode != expected_mode:
                raise ContractError(
                    "Receipt authority validation mode was reclassified"
                )
            n = scenario["n"]
            if scenario["complete_count"] != n:
                raise ContractError("Receipt authority evidence is incomplete")
            if mode == "baseline":
                if scenario["observed"] != 0 or scenario["v0_complete_count"] != 0:
                    raise ContractError("Receipt baseline evidence is invalid")
            elif (
                scenario["observed"] < scenario["k"]
                or scenario["v0_complete_count"] != n
                or scenario["v0_observed"] != 0
            ):
                raise ContractError("Receipt issue-v0 discrimination is invalid")


def build_validation_receipt(
    *,
    mode: str,
    evidence: Mapping[str, Any],
    clean_snapshot: BlobRecord,
    issuer: Mapping[str, Any],
    trusted_policy_manifest: Mapping[str, Any],
    policy_commit_sha: str,
    policy_content_digest: str,
    review: Mapping[str, Any],
    targeted_verification: Mapping[str, Any],
    continuous_integration: Mapping[str, Any],
    issued_at: str,
) -> dict[str, Any]:
    if mode not in {"shadow", "merge"}:
        raise ContractError("Validation receipt mode is invalid")
    validate_evidence(evidence)
    lifecycle = clean_snapshot.value
    if (
        lifecycle.get("snapshot_type") != "clean"
        or lifecycle.get("state") != "CLEAN"
        or lifecycle.get("digests", {}).get("evidence_digest")
        != evidence["evidence_digest"]
        or lifecycle.get("cleanup", {}).get("exact_clean") is not True
        or lifecycle.get("cycle_id") != evidence["cycle_id"]
        or lifecycle.get("epoch") != evidence["epoch"]
        or lifecycle.get("repository") != evidence["repository"]
        or lifecycle.get("pr_number") != evidence["pr_number"]
    ):
        raise ContractError("Validation receipt requires immutable CLEAN evidence")
    final_head = lifecycle["git"]["final_head_sha"]
    final_tree = lifecycle["git"]["final_tree_sha"]
    if not final_head or not final_tree:
        raise ContractError("Validation CLEAN snapshot lacks final Git identity")
    if evidence["candidate_head_sha"] != final_head:
        raise ContractError("Validation evidence is not bound to final head")
    merge = mode == "merge"
    policy_ref = (
        trusted_policy_manifest["workflow"]["required_ref"]
        if merge
        else final_head
    )
    allowed_workflow = (
        trusted_policy_manifest["workflow"]["receipt_path"]
        if merge
        else trusted_policy_manifest["workflow"]["candidate_path"]
    )
    scope = lifecycle["scope_freeze"]
    authorities = [
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
    value = {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-receipt",
        "mode": mode,
        "authorizes_merge": merge,
        "repository": evidence["repository"],
        "pr_number": evidence["pr_number"],
        "cycle_id": evidence["cycle_id"],
        "epoch": evidence["epoch"],
        "final_head_sha": final_head,
        "final_tree_sha": final_tree,
        "issued_at": issued_at,
        "issuer": dict(issuer),
        "trusted_policy": {
            "default_branch_trust_anchor_present": merge,
            "repository": trusted_policy_manifest["repository"],
            "path": trusted_policy_manifest["policy_path"],
            "ref": policy_ref,
            "commit_sha": policy_commit_sha if merge else final_head,
            "content_digest": policy_content_digest,
            "allowed_workflow_path": allowed_workflow,
            "allowed_app_id": trusted_policy_manifest["issuer"]["app_id"],
            "allowed_app_slug": trusted_policy_manifest["issuer"]["app_slug"],
            "required_environment": trusted_policy_manifest[
                "protected_environment"
            ],
            "required_check": trusted_policy_manifest["checks"][
                "required_check"
            ],
        },
        "scope_freeze": {
            "head_sha": scope["head_sha"],
            "tree_sha": scope["tree_sha"],
            "frozen_at": scope["frozen_at"],
            "source_tree_digest": scope["source_tree_digest"],
            "validation_contract_digest": scope[
                "validation_contract_digest"
            ],
            "scope_digest": content_hash(scope),
        },
        "review": dict(review),
        "required_ci": {
            "targeted_verification": dict(targeted_verification),
            "continuous_integration": dict(continuous_integration),
        },
        "catalog_hashes": dict(evidence["catalog_hashes"]),
        "artifact_manifest_hash": evidence["artifact_manifest_hash"],
        "source_tree_digest": evidence["source_tree_digest"],
        "validation_contract_digest": evidence["validation_contract_digest"],
        "execution_matrix_digest": evidence["execution_matrix_digest"],
        "runtime_topology_digest": evidence["runtime_topology_digest"],
        "quota_plan_digest": evidence["quota_plan_digest"],
        "evidence_digest": evidence["evidence_digest"],
        "telemetry_resource_set": evidence["telemetry_resource_set"],
        "test_agent_model": dict(evidence["test_agent_model"]),
        "authorities": authorities,
        "clean_snapshot": {
            "path": f"{clean_snapshot.container}/{clean_snapshot.name}",
            "version_id": clean_snapshot.version_id,
            "etag": clean_snapshot.etag,
            "digest": lifecycle["journal_digest"],
            "cleanup_plan_hash": lifecycle["cleanup"]["plan_hash"],
            "exact_clean": True,
            "verified_at": lifecycle["cleanup"]["verification_at"],
        },
        "receipt_digest": "",
    }
    result = stamp_receipt_digest(value)
    validate_receipt(result)
    return result


def verify_merge_provenance(
    receipt: Mapping[str, Any],
    *,
    trusted_policy: Mapping[str, Any],
    queried: QueriedGitHubState,
) -> None:
    validate_receipt(receipt)
    if receipt["mode"] != "merge" or receipt["authorizes_merge"] is not True:
        raise ContractError("Only a merge receipt can enter protected verification")
    final_head = receipt["final_head_sha"]
    if queried.pr_head_sha != final_head or queried.pr_tree_sha != receipt["final_tree_sha"]:
        raise ContractError("Pull request head changed before receipt issuance")
    if queried.default_branch_ref != "refs/heads/main":
        raise ContractError("Trusted policy was not read from the default branch")
    policy = receipt["trusted_policy"]
    if (
        policy["default_branch_trust_anchor_present"] is not True
        or policy["repository"] != trusted_policy["repository"]
        or policy["path"] != trusted_policy["policy_path"]
        or policy["ref"] != trusted_policy["workflow"]["required_ref"]
        or policy["commit_sha"] != queried.policy_commit_sha
        or policy["content_digest"] != queried.policy_content_digest
        or policy["content_digest"] != content_hash(trusted_policy)
        or policy["allowed_workflow_path"]
        != trusted_policy["workflow"]["receipt_path"]
        or policy["allowed_app_id"] != trusted_policy["issuer"]["app_id"]
        or policy["allowed_app_slug"] != trusted_policy["issuer"]["app_slug"]
    ):
        raise ContractError("Default-branch trusted policy proof is invalid")
    required_names = {
        trusted_policy["checks"]["comprehensive_review"],
        trusted_policy["checks"]["targeted_verification"],
        trusted_policy["checks"]["continuous_integration"],
        trusted_policy["checks"]["required_check"],
    }
    if not required_names.issubset(set(queried.required_checks)):
        raise ContractError("Protected branch required-check policy is incomplete")
    records = [
        (
            "comprehensive_review",
            receipt["review"]["check"],
            trusted_policy["checks"]["comprehensive_review"],
            trusted_policy["workflow"]["review_path"],
            receipt["scope_freeze"]["head_sha"],
        ),
        (
            "targeted_verification",
            receipt["required_ci"]["targeted_verification"],
            trusted_policy["checks"]["targeted_verification"],
            trusted_policy["workflow"]["targeted_path"],
            final_head,
        ),
        (
            "continuous_integration",
            receipt["required_ci"]["continuous_integration"],
            trusted_policy["checks"]["continuous_integration"],
            trusted_policy["workflow"]["ci_path"],
            final_head,
        ),
    ]
    checks_by_id = {item.check_run_id: item for item in queried.checks}
    completed: dict[str, datetime] = {}
    for role, record, expected_name, expected_path, expected_head in records:
        if record is None:
            raise ContractError("Protected receipt is missing a required check")
        live = checks_by_id.get(record["check_run_id"])
        if live is None or (
            record["name"] != expected_name
            or live.name != record["name"]
            or live.check_suite_id != record["check_suite_id"]
            or live.app_id != record["app_id"]
            or live.app_slug != record["app_slug"]
            or live.workflow_id != record["workflow_id"]
            or live.workflow_path != record["workflow_path"]
            or live.workflow_sha != record["workflow_sha"]
            or live.completed_at != record["completed_at"]
            or record["workflow_path"] != expected_path
            or record["workflow_sha"] != expected_head
            or live.head_sha != expected_head
            or live.conclusion != "success"
            or live.result_digest != record["result_digest"]
            or record["app_id"] != trusted_policy["issuer"]["app_id"]
            or record["app_slug"] != trusted_policy["issuer"]["app_slug"]
        ):
            raise ContractError(f"Protected check proof is stale: {record['name']}")
        completed[role] = _parse_time(record["completed_at"], record["name"])
    frozen_at = _parse_time(
        receipt["scope_freeze"]["frozen_at"],
        "scope freeze",
    )
    issued_at = _parse_time(receipt["issued_at"], "receipt issuance")
    if (
        completed["comprehensive_review"] < frozen_at
        or completed["targeted_verification"]
        < completed["comprehensive_review"]
        or completed["continuous_integration"]
        < completed["comprehensive_review"]
        or issued_at < max(completed.values())
    ):
        raise ContractError("Protected check completion ordering is invalid")
    issuer = receipt["issuer"]
    workflow = queried.issuer_workflow
    if (
        issuer["environment"] != trusted_policy["protected_environment"]
        or issuer["app_id"] != trusted_policy["issuer"]["app_id"]
        or issuer["app_slug"] != trusted_policy["issuer"]["app_slug"]
        or issuer["workflow_database_id"] != workflow.workflow_id
        or issuer["workflow_path"] != workflow.workflow_path
        or issuer["workflow_path"] != trusted_policy["workflow"]["receipt_path"]
        or issuer["workflow_ref"] != trusted_policy["workflow"]["required_ref"]
        or issuer["workflow_commit_sha"] != workflow.workflow_sha
        or issuer["run_id"] != workflow.run_id
        or issuer["run_attempt"] != workflow.run_attempt
        or workflow.head_sha != queried.policy_commit_sha
    ):
        raise ContractError("Protected receipt issuer provenance is invalid")


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ContractError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ContractError(f"{label} timestamp lacks a timezone")
    return parsed.astimezone(UTC)


class ReceiptIssuer:
    def __init__(
        self,
        store: ReceiptBlobStore,
        *,
        mirror_root: Path | None = None,
    ) -> None:
        self._store = store
        self._mirror_root = mirror_root or (
            runtime_root() / "test-agent-validation" / "receipts"
        )

    def issue_shadow(
        self,
        receipt: Mapping[str, Any],
        *,
        candidate_policy: Mapping[str, Any] | None = None,
    ) -> BlobRecord:
        validate_receipt(receipt)
        if (
            receipt["mode"] != "shadow"
            or receipt["authorizes_merge"] is not False
            or receipt["trusted_policy"]["default_branch_trust_anchor_present"]
            is not False
        ):
            raise ContractError("Shadow receipt cannot authorize merge")
        policy = dict(candidate_policy or read_yaml(TRUSTED_POLICY_PATH))
        trusted = receipt["trusted_policy"]
        issuer = receipt["issuer"]
        if (
            trusted["repository"] != receipt["repository"]
            or trusted["ref"] != receipt["final_head_sha"]
            or trusted["commit_sha"] != receipt["final_head_sha"]
            or trusted["content_digest"] != content_hash(policy)
            or trusted["allowed_workflow_path"]
            != policy["workflow"]["candidate_path"]
            or issuer["workflow_path"] != policy["workflow"]["candidate_path"]
            or issuer["workflow_ref"] != receipt["final_head_sha"]
            or issuer["workflow_commit_sha"] != receipt["final_head_sha"]
            or issuer["environment"] != "test-agent-validation-shadow"
            or issuer["issuer_code_digest"] != current_issuer_code_digest()
        ):
            raise ContractError("Shadow receipt candidate-head provenance is invalid")
        self._verify_clean_proof(receipt)
        return self._write(receipt)

    def issue_merge(
        self,
        receipt: Mapping[str, Any],
        *,
        trusted_policy: Mapping[str, Any],
        reader: GitHubStateReader,
        oidc_subject: str,
        environment: Mapping[str, str],
    ) -> BlobRecord:
        verify_runtime_issuer(
            receipt,
            oidc_subject=oidc_subject,
            environment=environment,
        )
        queried = reader.read(
            repository=receipt["repository"],
            pr_number=receipt["pr_number"],
            final_head_sha=receipt["final_head_sha"],
            policy_path=trusted_policy["policy_path"],
            default_branch=trusted_policy["default_branch"],
            issuer_run_id=receipt["issuer"]["run_id"],
            review_head_sha=receipt["scope_freeze"]["head_sha"],
        )
        verify_merge_provenance(
            receipt,
            trusted_policy=trusted_policy,
            queried=queried,
        )
        self._verify_clean_proof(receipt)
        return self._write(receipt)

    def _verify_clean_proof(self, receipt: Mapping[str, Any]) -> None:
        reference = receipt["clean_snapshot"]
        path = str(reference["path"])
        if "/" not in path:
            raise ContractError("Receipt CLEAN snapshot path is invalid")
        container, name = path.split("/", 1)
        if container != "test-agent-validation-snapshots":
            raise ContractError("Receipt CLEAN snapshot container is invalid")
        clean = self._store.read(
            container,
            name,
            version_id=reference["version_id"],
        )
        if (
            clean.version_id != reference["version_id"]
            or clean.etag != reference["etag"]
            or clean.value.get("journal_digest") != reference["digest"]
            or clean.value.get("snapshot_type") != "clean"
            or clean.value.get("state") != "CLEAN"
            or clean.value.get("cycle_id") != receipt["cycle_id"]
            or clean.value.get("epoch") != receipt["epoch"]
            or clean.value.get("cleanup", {}).get("plan_hash")
            != reference["cleanup_plan_hash"]
            or clean.value.get("cleanup", {}).get("exact_clean") is not True
            or clean.value.get("cleanup", {}).get("residue_ids") != []
            or clean.value.get("digests", {}).get("evidence_digest")
            != receipt["evidence_digest"]
            or clean.value.get("git", {}).get("final_head_sha")
            != receipt["final_head_sha"]
            or clean.value.get("git", {}).get("final_tree_sha")
            != receipt["final_tree_sha"]
        ):
            raise ContractError("Immutable CLEAN snapshot proof is invalid")
        evidence_reference = clean.value.get("evidence_reference")
        if not isinstance(evidence_reference, dict):
            raise ContractError("Immutable CLEAN snapshot has no evidence reference")
        evidence_path = str(evidence_reference.get("path") or "")
        if "/" not in evidence_path:
            raise ContractError("Validation evidence snapshot path is invalid")
        evidence_container, evidence_name = evidence_path.split("/", 1)
        if evidence_container != "test-agent-validation-snapshots":
            raise ContractError("Validation evidence snapshot container is invalid")
        evidence = self._store.read(
            evidence_container,
            evidence_name,
            version_id=evidence_reference["version_id"],
        )
        if (
            evidence.version_id != evidence_reference["version_id"]
            or evidence.etag != evidence_reference["etag"]
            or evidence.value.get("evidence_digest")
            != evidence_reference["digest"]
            or evidence_reference["digest"] != receipt["evidence_digest"]
        ):
            raise ContractError("Immutable validation evidence reference is invalid")
        validate_evidence(evidence.value)
        expected_summaries = [
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
            for authority in evidence.value["authorities"]
        ]
        if receipt["authorities"] != expected_summaries:
            raise ContractError(
                "Receipt authority summaries do not match immutable evidence"
            )
        active = self._store.read(
            "test-agent-validation-lifecycle",
            "active.json",
        )
        if (
            active.value.get("cycle_id") != receipt["cycle_id"]
            or active.value.get("epoch") != receipt["epoch"]
            or active.value.get("state") not in {"CLEAN", "RECEIPT_ISSUED"}
            or active.value.get("clean_snapshot") != {
                "path": reference["path"],
                "version_id": reference["version_id"],
                "etag": reference["etag"],
                "digest": reference["digest"],
            }
        ):
            raise ContractError("Active lifecycle does not reference immutable CLEAN proof")
        if (
            active.value.get("state") == "RECEIPT_ISSUED"
            and active.value.get("receipt_reference", {}).get("digest")
            != receipt["receipt_digest"]
        ):
            raise ContractError("Active lifecycle references another receipt")

    def _write(self, receipt: Mapping[str, Any]) -> BlobRecord:
        repository = receipt["repository"]
        owner, name = repository.split("/", 1)
        if receipt["mode"] == "merge":
            path = (
                f"receipts/{owner}/{name}/{receipt['pr_number']}/"
                f"{receipt['final_head_sha']}/test-agent-validation-receipt.json"
            )
        else:
            path = (
                f"shadow-receipts/{owner}/{name}/{receipt['pr_number']}/"
                f"{receipt['cycle_id']}/{receipt['final_head_sha']}/"
                "test-agent-validation-receipt.json"
            )
        value = dict(receipt)
        container = (
            MERGE_RECEIPT_CONTAINER
            if receipt["mode"] == "merge"
            else SHADOW_RECEIPT_CONTAINER
        )
        record = self._store.create_once(container, path, value)
        if record.value["receipt_digest"] != receipt["receipt_digest"]:
            raise ContractError("Create-once receipt retry found a different digest")
        immutable_json(self._mirror_root / path, value)
        return record


class GhGitHubStateReader:
    def __init__(self, token: str) -> None:
        if not token:
            raise ContractError("Scoped GitHub token is required")
        self._token = token

    def read(
        self,
        *,
        repository: str,
        pr_number: int,
        final_head_sha: str,
        policy_path: str,
        default_branch: str,
        issuer_run_id: int,
        review_head_sha: str,
    ) -> QueriedGitHubState:
        pull = self._api(f"repos/{repository}/pulls/{pr_number}")
        commit = self._api(f"repos/{repository}/git/commits/{final_head_sha}")
        final_checks_payload = self._api(
            f"repos/{repository}/commits/{final_head_sha}/check-runs?per_page=100"
        )
        review_checks_payload = (
            final_checks_payload
            if review_head_sha == final_head_sha
            else self._api(
                f"repos/{repository}/commits/{review_head_sha}/"
                "check-runs?per_page=100"
            )
        )
        action_runs = []
        for head in {final_head_sha, review_head_sha}:
            runs_payload = self._api(
                f"repos/{repository}/actions/runs?head_sha={head}&per_page=100"
            )
            action_runs.extend(
                item
                for item in runs_payload.get("workflow_runs", [])
                if isinstance(item, dict)
            )
        run_by_suite = {
            int(item["check_suite_id"]): item
            for item in action_runs
            if item.get("check_suite_id") is not None
        }
        protection = self._api(
            f"repos/{repository}/branches/{default_branch}/protection/required_status_checks"
        )
        policy_payload = self._api(
            f"repos/{repository}/contents/{policy_path}?ref={default_branch}"
        )
        default_commit = self._api(
            f"repos/{repository}/commits/{default_branch}"
        )
        workflow_run = self._api(
            f"repos/{repository}/actions/runs/{issuer_run_id}"
        )
        workflow = self._api(
            f"repos/{repository}/actions/workflows/{workflow_run['workflow_id']}"
        )
        policy_content = base64.b64decode(
            str(policy_payload["content"]).replace("\n", "")
        )
        import yaml

        parsed_policy = yaml.safe_load(policy_content)
        if not isinstance(parsed_policy, dict):
            raise ContractError("Default-branch trusted policy is not an object")
        checks_by_id = {
            int(item["id"]): item
            for payload in (final_checks_payload, review_checks_payload)
            for item in payload.get("check_runs", [])
        }
        checks = []
        for item in checks_by_id.values():
            suite_id = int(item["check_suite"]["id"])
            run = run_by_suite.get(suite_id)
            if run is None:
                continue
            workflow_id = int(run["workflow_id"])
            workflow_value = self._api(
                f"repos/{repository}/actions/workflows/{workflow_id}"
            )
            checks.append(
                CheckState(
                    name=str(item["name"]),
                    check_run_id=int(item["id"]),
                    check_suite_id=suite_id,
                    app_id=int(item["app"]["id"]),
                    app_slug=str(item["app"]["slug"]),
                    head_sha=str(item["head_sha"]),
                    conclusion=str(item["conclusion"]),
                    result_digest=content_hash(
                        {
                            "conclusion": item.get("conclusion"),
                            "output": item.get("output"),
                        }
                    ),
                    workflow_id=workflow_id,
                    workflow_path=str(workflow_value["path"]),
                    workflow_sha=str(run["head_sha"]),
                    completed_at=str(item["completed_at"]),
                )
            )
        required = {
            str(item)
            for item in protection.get("contexts", [])
            if isinstance(item, str)
        }
        required.update(
            str(item["context"])
            for item in protection.get("checks", [])
            if isinstance(item, dict) and isinstance(item.get("context"), str)
        )
        run_attempt = workflow_run.get("run_attempt")
        return QueriedGitHubState(
            pr_head_sha=str(pull["head"]["sha"]),
            pr_tree_sha=str(commit["tree"]["sha"]),
            default_branch_ref=f"refs/heads/{default_branch}",
            policy_commit_sha=str(default_commit["sha"]),
            policy_content_digest=content_hash(parsed_policy),
            required_checks=tuple(sorted(required)),
            checks=tuple(checks),
            issuer_workflow=WorkflowState(
                workflow_id=int(workflow["id"]),
                workflow_path=str(workflow["path"]),
                workflow_sha=str(default_commit["sha"]),
                head_sha=str(workflow_run["head_sha"]),
                run_id=int(workflow_run["id"]),
                run_attempt=int(run_attempt if run_attempt is not None else 1),
            ),
        )

    def check_record(
        self,
        *,
        repository: str,
        head_sha: str,
        name: str,
    ) -> dict[str, Any]:
        matches = []
        for attempt in range(60):
            checks_payload = self._api(
                f"repos/{repository}/commits/{head_sha}/check-runs?per_page=100"
            )
            matches = [
                item
                for item in checks_payload.get("check_runs", [])
                if isinstance(item, dict)
                and item.get("name") == name
                and item.get("head_sha") == head_sha
                and item.get("conclusion") == "success"
            ]
            if len(matches) == 1:
                break
            if attempt < 59:
                time.sleep(15)
        if len(matches) != 1:
            raise ContractError(
                f"Expected one successful protected check named {name}"
            )
        item = matches[0]
        suite_id = int(item["check_suite"]["id"])
        runs = self._api(
            f"repos/{repository}/actions/runs?head_sha={head_sha}&per_page=100"
        )
        run_matches = [
            run
            for run in runs.get("workflow_runs", [])
            if isinstance(run, dict)
            and int(run.get("check_suite_id") or 0) == suite_id
        ]
        if len(run_matches) != 1:
            raise ContractError(
                f"Protected check {name} has no unique workflow run"
            )
        run = run_matches[0]
        workflow_id = int(run["workflow_id"])
        workflow = self._api(
            f"repos/{repository}/actions/workflows/{workflow_id}"
        )
        return asdict(
            CheckState(
                name=name,
                check_run_id=int(item["id"]),
                check_suite_id=suite_id,
                app_id=int(item["app"]["id"]),
                app_slug=str(item["app"]["slug"]),
                head_sha=head_sha,
                conclusion="success",
                result_digest=content_hash(
                    {
                        "conclusion": item.get("conclusion"),
                        "output": item.get("output"),
                    }
                ),
                workflow_id=workflow_id,
                workflow_path=str(workflow["path"]),
                workflow_sha=str(run["head_sha"]),
                completed_at=str(item["completed_at"]),
            )
        )

    def workflow_state(
        self,
        *,
        repository: str,
        run_id: int,
    ) -> WorkflowState:
        run = self._api(f"repos/{repository}/actions/runs/{run_id}")
        workflow_id = int(run["workflow_id"])
        workflow = self._api(
            f"repos/{repository}/actions/workflows/{workflow_id}"
        )
        return WorkflowState(
            workflow_id=workflow_id,
            workflow_path=str(workflow["path"]),
            workflow_sha=str(run["head_sha"]),
            head_sha=str(run["head_sha"]),
            run_id=int(run["id"]),
            run_attempt=int(run.get("run_attempt") or 1),
        )

    def _api(self, path: str) -> dict[str, Any]:
        process = subprocess.run(
            ["gh", "api", path],
            env={**os.environ, "GH_TOKEN": self._token},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if process.returncode != 0:
            raise ContractError("Protected GitHub state query failed")
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise ContractError("Protected GitHub state query returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ContractError("Protected GitHub state query returned no object")
        return value


def publish_required_check(
    receipt: Mapping[str, Any],
    *,
    trusted_policy: Mapping[str, Any],
    token: str,
) -> dict[str, Any]:
    if not token:
        raise ContractError("Scoped GitHub token is required for the merge check")
    if receipt.get("mode") != "merge" or receipt.get("authorizes_merge") is not True:
        raise ContractError("Only a merge receipt can publish the required check")
    payload = {
        "name": trusted_policy["checks"]["required_check"],
        "head_sha": receipt["final_head_sha"],
        "status": "completed",
        "conclusion": "success",
        "external_id": receipt["receipt_digest"],
        "output": {
            "title": "Test Agent Validation complete",
            "summary": (
                "Protected receipt, 41/41 evidence, and immutable CLEAN proof "
                "were verified."
            ),
        },
    }
    process = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{receipt['repository']}/check-runs",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "GH_TOKEN": token},
    )
    if process.returncode != 0:
        raise ContractError("Protected required-check publication failed")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("Protected required-check response is invalid") from error
    app = result.get("app") if isinstance(result, dict) else None
    if (
        not isinstance(app, dict)
        or result.get("name") != trusted_policy["checks"]["required_check"]
        or result.get("head_sha") != receipt["final_head_sha"]
        or result.get("conclusion") != "success"
        or int(app.get("id") or 0) != trusted_policy["issuer"]["app_id"]
        or str(app.get("slug") or "") != trusted_policy["issuer"]["app_slug"]
    ):
        raise ContractError("Protected required check used an unexpected App identity")
    return {
        "check_run_id": int(result["id"]),
        "head_sha": str(result["head_sha"]),
        "app_id": int(app["id"]),
        "app_slug": str(app["slug"]),
    }
