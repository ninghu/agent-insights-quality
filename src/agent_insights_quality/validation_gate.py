from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.live import LiveRuntime, _azure_cli_token
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import (
    ContractError,
    content_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_blob import AzureValidationBlobStore
from agent_insights_quality.validation_cleanup_azure import (
    AzureValidationCleanupBackend,
)
from agent_insights_quality.validation_credentials import (
    validation_blob_credential,
)
from agent_insights_quality.validation_cycle import (
    ValidationCycleController,
    initial_lifecycle,
)
from agent_insights_quality.validation_execution import (
    cleanup_validation_cycle,
    execute_initial_candidate_pass,
)
from agent_insights_quality.validation_issuer import (
    GhGitHubStateReader,
    ReceiptIssuer,
    build_validation_receipt,
    current_issuer_code_digest,
    github_actions_oidc_subject,
    oidc_subject_digest,
    publish_review_attestation,
    publish_required_check,
    select_provenance_check,
)
from agent_insights_quality.validation_lifecycle import (
    ACTIVE_BLOB,
    ACTIVE_CONTAINER,
    LifecycleJournal,
    validate_lifecycle,
)
from agent_insights_quality.validation_live import FoundryScenarioAttemptRunner
from agent_insights_quality.validation_manifest import (
    authority_specs,
    validate_candidate_manifest,
    validation_authority_cost,
    validation_endpoint_costs,
)
from agent_insights_quality.validation_policy import (
    load_trusted_policy,
    load_validation_policy,
)
from agent_insights_quality.validation_provisioning import (
    FoundryAuthorityDeployer,
    ValidationProjectProvisioner,
    measure_test_agent_capacity,
    prepare_validation_support_images,
    validation_runtime_profile,
)
from agent_insights_quality.validation_quota import (
    EndpointCost,
    ValidationScheduler,
    WeightedTokenBucket,
    build_capacity_plan,
)


def run_validation_gate(
    *,
    candidate_path: Path,
    storage_account: str,
    expected_azure_client_id: str,
    automation_principal_id: str,
    receipt_output: Path,
    github_token: str,
    mode: str = "shadow",
    environment: Mapping[str, str] | None = None,
    now: Any = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    if mode not in {"shadow", "merge"}:
        raise ContractError("Validation gate mode must be shadow or merge")
    values = environment or os.environ
    blob_credential = validation_blob_credential(
        expected_azure_client_id,
        automation_principal_id,
    )
    if not github_token:
        raise ContractError("Scoped GitHub token is required")
    candidate = read_json(candidate_path)
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    trusted_policy, trusted_digest = load_trusted_policy()
    validate_candidate_manifest(
        candidate,
        agents=agents,
        issues=issues,
        policy=policy,
    )
    authorities = authority_specs(agents, issues)
    base_profile = RuntimeProfile.from_env("staging", "g29")
    profile = validation_runtime_profile(
        candidate["project_name"],
        cycle_id=candidate["cycle_id"],
        base=base_profile,
    )
    store = AzureValidationBlobStore(
        storage_account,
        credential=blob_credential,
    )
    lease_id = str(uuid.uuid4())
    ownership_nonce = uuid.uuid4().hex
    workflow_reference = str(values.get("GITHUB_WORKFLOW_REF") or "")
    run_reference = (
        f"{values.get('GITHUB_RUN_ID', '')}."
        f"{values.get('GITHUB_RUN_ATTEMPT', '')}"
    )
    if not workflow_reference or run_reference in {".", ""}:
        raise ContractError("GitHub workflow identity is missing")
    lifecycle = initial_lifecycle(
        candidate,
        policy=policy,
        policy_manifest=trusted_policy,
        policy_manifest_digest=trusted_digest,
        policy_commit_sha=candidate["candidate_head_sha"],
        policy_ref=candidate["candidate_head_sha"],
        lease_id=lease_id,
        ownership_nonce=ownership_nonce,
        holder_workflow_reference=workflow_reference,
        holder_app_reference=trusted_policy["issuer"]["app_slug"],
        holder_run_reference=run_reference,
        account_reference=content_hash(
            {"account_resource_id": profile.account_resource_id}
        ),
        now=now(),
    )
    journal = LifecycleJournal(
        store,
        policy,
        mirror_root=runtime_root()
        / "test-agent-validation"
        / "lifecycle",
    )
    lease_id, active = journal.begin_cycle(
        lifecycle,
        proposed_lease_id=lease_id,
    )
    controller = ValidationCycleController(
        journal,
        lease_id=lease_id,
        active=active,
    )
    measurement = measure_test_agent_capacity(profile, now=now)
    costs = validation_endpoint_costs(authorities)
    capacity = build_capacity_plan(
        measurement,
        policy=policy,
        costs=costs,
    )
    scheduler = ValidationScheduler(
        capacity,
        WeightedTokenBucket(
            request_capacity=capacity.available_rpm,
            token_capacity=capacity.available_tpm,
        ),
    )
    project_provisioner = ValidationProjectProvisioner(
        profile,
        automation_principal_id=automation_principal_id,
        policy=policy,
    )
    support_agent = next(
        item
        for item in agents["agents"]
        if item["name"] == "support-ticket-agent"
    )

    def resource_event(event: dict[str, Any]) -> None:
        controller.dynamic_resource_event(event, now=now())

    def deployer_factory(project):
        support = prepare_validation_support_images(
            profile,
            support_agent,
            cycle_id=candidate["cycle_id"],
            record_resource=resource_event,
        )
        return FoundryAuthorityDeployer(
            profile=profile,
            agent_catalog=agents,
            issue_catalog=issues,
            token_provider=_azure_cli_token,
            project=project,
            support_images=support.images,
        )

    runner = FoundryScenarioAttemptRunner(
        LiveRuntime(profile),
        endpoint_costs=_authority_costs(authorities),
        stabilization_seconds=180,
        record_resource=resource_event,
        now=now,
    )
    try:
        evidence, _ = execute_initial_candidate_pass(
            candidate=candidate,
            authorities=authorities,
            capacity_plan=capacity,
            controller=controller,
            project_provisioner=project_provisioner,
            deployer_factory=deployer_factory,
            runner=runner,
            scheduler=scheduler,
            evidence_store=store,
            policy_manifest_digest=trusted_digest,
            policy=policy,
            model_contract=agents["models"]["test_agents"],
            now=now,
        )
        reader = GhGitHubStateReader(github_token)
        if mode == "merge":
            review_check = reader.check_record(
                repository=candidate["repository"],
                head_sha=candidate["candidate_head_sha"],
                name=trusted_policy["checks"]["comprehensive_review"],
                expected_workflow_path=trusted_policy["workflow"]["review_path"],
                expected_app_id=trusted_policy["issuer"]["app_id"],
                expected_app_slug=trusted_policy["issuer"]["app_slug"],
                expected_cycle_id=candidate["cycle_id"],
            )
            controller.record_review(
                mode="comprehensive",
                check_reference=str(review_check["check_run_id"]),
                findings_digest=review_check["result_digest"],
                now=now(),
            )
        else:
            controller.record_review(
                mode="shadow_skipped",
                check_reference=None,
                findings_digest=None,
                now=now(),
            )
        controller.begin_revalidation(
            head_sha=candidate["candidate_head_sha"],
            tree_sha=candidate["candidate_tree_sha"],
            changed_authority_ids=[],
            shared_contract_changed=False,
            now=now(),
        )
        evidence_reference = controller.active.value["evidence_reference"]
        evidence_record = store.read(
            *str(evidence_reference["path"]).split("/", 1),
            version_id=evidence_reference["version_id"],
        )
        controller.final_checks(
            final_head_sha=candidate["candidate_head_sha"],
            final_tree_sha=candidate["candidate_tree_sha"],
            evidence=evidence_record,
            now=now(),
        )
        targeted = reader.check_record(
            repository=candidate["repository"],
            head_sha=candidate["candidate_head_sha"],
            name=trusted_policy["checks"]["targeted_verification"],
            expected_workflow_path=trusted_policy["workflow"]["targeted_path"],
            expected_app_id=trusted_policy["issuer"]["app_id"],
            expected_app_slug=trusted_policy["issuer"]["app_slug"],
        )
        ci = reader.check_record(
            repository=candidate["repository"],
            head_sha=candidate["candidate_head_sha"],
            name=trusted_policy["checks"]["continuous_integration"],
            expected_workflow_path=trusted_policy["workflow"]["ci_path"],
            expected_app_id=trusted_policy["issuer"]["app_id"],
            expected_app_slug=trusted_policy["issuer"]["app_slug"],
        )
        cleanup_backend = AzureValidationCleanupBackend(
            profile=profile,
            runtime_topology=controller.active.value["runtime_topology"],
            resources=controller.active.value["resources"],
            token_provider=_azure_cli_token,
        )
        clean = cleanup_validation_cycle(
            controller=controller,
            backend=cleanup_backend,
            policy=policy,
            failed_cycle=False,
            now=now,
        )
        output = receipt_output.expanduser().resolve()
        private = runtime_root()
        if output != private and private not in output.parents:
            raise ContractError("Validation receipt output must stay private")
        if mode == "merge":
            handoff = {
                "schema_version": "1.0.0",
                "kind": "test-agent-validation-merge-handoff",
                "cycle_id": candidate["cycle_id"],
                "repository": candidate["repository"],
                "pr_number": candidate["pr_number"],
                "final_head_sha": candidate["candidate_head_sha"],
                "final_tree_sha": candidate["candidate_tree_sha"],
                "clean_snapshot": controller.active.value["clean_snapshot"],
            }
            immutable_json(output, handoff)
            return {
                "cycle_id": candidate["cycle_id"],
                "state": "CLEAN",
                "handoff": str(output),
                "final_head_sha": candidate["candidate_head_sha"],
            }
        workflow = reader.workflow_state(
            repository=candidate["repository"],
            run_id=int(values["GITHUB_RUN_ID"]),
        )
        subject = github_actions_oidc_subject(values)
        issuer = {
            "environment": "test-agent-validation-shadow",
            "oidc_subject_digest": oidc_subject_digest(subject),
            "app_id": trusted_policy["issuer"]["app_id"],
            "app_slug": trusted_policy["issuer"]["app_slug"],
            "workflow_database_id": workflow.workflow_id,
            "workflow_path": workflow.workflow_path,
            "workflow_ref": candidate["candidate_head_sha"],
            "workflow_commit_sha": candidate["candidate_head_sha"],
            "run_id": workflow.run_id,
            "run_attempt": workflow.run_attempt,
            "issuer_code_digest": current_issuer_code_digest(),
        }
        review = {
            "status": "skipped",
            "comprehensive_review_count": 0,
            "exercised_requirements": [
                "lifecycle",
                "targeted_verification",
                "continuous_integration",
                "exact_cleanup",
            ],
            "missing_requirements": [
                "comprehensive_review",
                "default_branch_trust_anchor",
            ],
            "check": None,
            "findings_digest": None,
        }
        receipt = build_validation_receipt(
            mode="shadow",
            evidence=evidence,
            clean_snapshot=clean,
            issuer=issuer,
            trusted_policy_manifest=trusted_policy,
            policy_commit_sha=candidate["candidate_head_sha"],
            policy_content_digest=trusted_digest,
            review=review,
            targeted_verification=targeted,
            continuous_integration=ci,
            issued_at=now().astimezone(UTC).isoformat(),
        )
        immutable_json(output, receipt)
        receipt_record = ReceiptIssuer(store).issue_shadow(
            receipt,
            candidate_policy=trusted_policy,
        )
        controller.receipt_issued(receipt_record, now=now())
        return {
            "cycle_id": candidate["cycle_id"],
            "state": "RECEIPT_ISSUED",
            "receipt": str(output),
            "receipt_digest": receipt["receipt_digest"],
        }
    except (ContractError, OSError, RuntimeError):
        if controller.active.value["state"] == "CLEANING":
            cleanup_backend = AzureValidationCleanupBackend(
                profile=profile,
                runtime_topology=controller.active.value["runtime_topology"],
                resources=controller.active.value["resources"],
                token_provider=_azure_cli_token,
            )
            cleanup_validation_cycle(
                controller=controller,
                backend=cleanup_backend,
                policy=policy,
                failed_cycle=True,
                now=now,
            )
        raise


def run_shadow_gate(
    *,
    candidate_path: Path,
    storage_account: str,
    expected_azure_client_id: str,
    automation_principal_id: str,
    receipt_output: Path,
    github_token: str,
    environment: Mapping[str, str] | None = None,
    now: Any = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    return run_validation_gate(
        candidate_path=candidate_path,
        storage_account=storage_account,
        expected_azure_client_id=expected_azure_client_id,
        automation_principal_id=automation_principal_id,
        receipt_output=receipt_output,
        github_token=github_token,
        mode="shadow",
        environment=environment,
        now=now,
    )


def attest_frozen_review(
    *,
    storage_account: str,
    expected_azure_client_id: str,
    expected_azure_object_id: str,
    cycle_id: str,
    frozen_head_sha: str,
    findings_digest: str,
    github_token: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = environment or os.environ
    credential = validation_blob_credential(
        expected_azure_client_id,
        expected_azure_object_id,
    )
    store = AzureValidationBlobStore(
        storage_account,
        credential=credential,
    )
    active = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    validate_lifecycle(active.value)
    if (
        active.value["state"] != "FROZEN"
        or active.value["cycle_id"] != cycle_id
        or active.value["scope_freeze"]["head_sha"] != frozen_head_sha
        or len(findings_digest) != 71
        or not findings_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in findings_digest[7:])
    ):
        raise ContractError(
            "Review attestation does not match the frozen validation lifecycle"
        )
    trusted_policy, _ = load_trusted_policy()
    result = publish_review_attestation(
        repository=active.value["repository"],
        frozen_head_sha=frozen_head_sha,
        cycle_id=cycle_id,
        findings_digest=findings_digest,
        trusted_policy=trusted_policy,
        token=github_token,
        environment=values,
    )
    return {
        **result,
        "scope_digest": content_hash(active.value["scope_freeze"]),
    }


def construct_and_issue_merge_receipt(
    *,
    storage_account: str,
    expected_azure_client_id: str,
    expected_azure_object_id: str,
    cycle_id: str,
    final_head_sha: str,
    candidate_root: Path,
    receipt_output: Path,
    github_token: str,
    environment: Mapping[str, str] | None = None,
    now: Any = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    values = environment or os.environ
    blob_credential = validation_blob_credential(
        expected_azure_client_id,
        expected_azure_object_id,
    )
    if not github_token:
        raise ContractError("Scoped GitHub token is required")
    candidate_root = candidate_root.resolve()
    _verify_candidate_checkout(candidate_root, final_head_sha)
    store = AzureValidationBlobStore(
        storage_account,
        credential=blob_credential,
    )
    active = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    validate_lifecycle(active.value)
    lifecycle = active.value
    if (
        lifecycle["state"] != "CLEAN"
        or lifecycle["cycle_id"] != cycle_id
        or lifecycle["git"]["final_head_sha"] != final_head_sha
    ):
        raise ContractError("Protected receipt lifecycle does not match the request")
    review_state = lifecycle.get("review")
    if (
        not isinstance(review_state, Mapping)
        or review_state.get("mode") != "comprehensive"
        or review_state.get("head_sha") != lifecycle["scope_freeze"]["head_sha"]
    ):
        raise ContractError("Protected receipt requires lifecycle-bound review proof")
    clean_reference = lifecycle["clean_snapshot"]
    evidence_reference = lifecycle["evidence_reference"]
    clean = store.read(
        *str(clean_reference["path"]).split("/", 1),
        version_id=clean_reference["version_id"],
    )
    evidence_record = store.read(
        *str(evidence_reference["path"]).split("/", 1),
        version_id=evidence_reference["version_id"],
    )
    trusted_policy, _ = load_trusted_policy()
    try:
        run_id = int(str(values.get("GITHUB_RUN_ID") or "0"))
    except ValueError as error:
        raise ContractError(
            "Protected receipt workflow run identity is invalid"
        ) from error
    if run_id < 1:
        raise ContractError("Protected receipt workflow run identity is missing")
    reader = GhGitHubStateReader(github_token)
    queried = reader.read(
        repository=lifecycle["repository"],
        pr_number=lifecycle["pr_number"],
        final_head_sha=final_head_sha,
        policy_path=trusted_policy["policy_path"],
        default_branch=trusted_policy["default_branch"],
        issuer_run_id=run_id,
        review_head_sha=lifecycle["scope_freeze"]["head_sha"],
    )
    def checked(name_key: str, workflow_key: str, head: str):
        return select_provenance_check(
            queried.checks,
            name=trusted_policy["checks"][name_key],
            workflow_path=trusted_policy["workflow"][workflow_key],
            head_sha=head,
            app_id=trusted_policy["issuer"]["app_id"],
            app_slug=trusted_policy["issuer"]["app_slug"],
        )

    review_check = checked(
        "comprehensive_review",
        "review_path",
        lifecycle["scope_freeze"]["head_sha"],
    )
    targeted_check = checked(
        "targeted_verification",
        "targeted_path",
        final_head_sha,
    )
    ci_check = checked(
        "continuous_integration",
        "ci_path",
        final_head_sha,
    )
    if (
        review_state.get("check_reference") != str(review_check.check_run_id)
        or review_state.get("findings_digest") != review_check.result_digest
    ):
        raise ContractError("Lifecycle review proof changed before receipt issuance")
    workflow = queried.issuer_workflow
    issuer = {
        "environment": trusted_policy["protected_environment"],
        "oidc_subject_digest": oidc_subject_digest(
            github_actions_oidc_subject(values)
        ),
        "app_id": trusted_policy["issuer"]["app_id"],
        "app_slug": trusted_policy["issuer"]["app_slug"],
        "workflow_database_id": workflow.workflow_id,
        "workflow_path": workflow.workflow_path,
        "workflow_ref": trusted_policy["workflow"]["required_ref"],
        "workflow_commit_sha": workflow.workflow_sha,
        "run_id": workflow.run_id,
        "run_attempt": workflow.run_attempt,
        "issuer_code_digest": current_issuer_code_digest(),
    }
    receipt = build_validation_receipt(
        mode="merge",
        evidence=evidence_record.value,
        clean_snapshot=clean,
        issuer=issuer,
        trusted_policy_manifest=trusted_policy,
        policy_commit_sha=queried.policy_commit_sha,
        policy_content_digest=queried.policy_content_digest,
        review={
            "status": "success",
            "comprehensive_review_count": 1,
            "exercised_requirements": [
                "comprehensive_review",
                "lifecycle",
                "targeted_verification",
                "continuous_integration",
                "exact_cleanup",
            ],
            "missing_requirements": [],
            "check": asdict(review_check),
            "findings_digest": review_check.result_digest,
        },
        targeted_verification=asdict(
            targeted_check
        ),
        continuous_integration=asdict(
            ci_check
        ),
        issued_at=now().astimezone(UTC).isoformat(),
        repository_root=candidate_root,
    )
    output = receipt_output.expanduser().resolve()
    private = runtime_root()
    if output != private and private not in output.parents:
        raise ContractError("Validation receipt output must stay private")
    immutable_json(output, receipt)
    record = ReceiptIssuer(store).issue_merge(
        receipt,
        trusted_policy=trusted_policy,
        reader=reader,
        oidc_subject=github_actions_oidc_subject(values),
        environment=values,
        repository_root=candidate_root,
    )
    controller = ValidationCycleController(
        LifecycleJournal(
            store,
            load_validation_policy(),
            mirror_root=private / "test-agent-validation" / "lifecycle",
        ),
        lease_id=active.value["lease"]["lease_id"],
        active=active,
    )
    controller.receipt_issued(record, now=now())
    required_check = publish_required_check(
        receipt,
        trusted_policy=trusted_policy,
        token=github_token,
    )
    return {
        "cycle_id": cycle_id,
        "state": "RECEIPT_ISSUED",
        "receipt": str(output),
        "receipt_digest": receipt["receipt_digest"],
        "required_check": required_check,
    }


def _verify_candidate_checkout(
    candidate_root: Path,
    final_head_sha: str,
) -> None:
    if not candidate_root.is_dir():
        raise ContractError("Exact candidate source checkout is missing")
    process = subprocess.run(
        ["git", "-C", str(candidate_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0 or process.stdout.strip() != final_head_sha:
        raise ContractError(
            "Candidate source checkout does not match the final head"
        )


def _authority_costs(
    authorities: list[Any],
) -> dict[str, EndpointCost]:
    return {
        authority.authority_id: validation_authority_cost(authority)
        for authority in authorities
    }
