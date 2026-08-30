from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
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
    verify_azure_service_principal,
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
)
from agent_insights_quality.validation_lifecycle import LifecycleJournal
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
    values = environment or os.environ
    verify_azure_service_principal(expected_azure_client_id)
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
    store = AzureValidationBlobStore(storage_account)
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
        reader = GhGitHubStateReader(github_token)
        targeted = reader.check_record(
            repository=candidate["repository"],
            head_sha=candidate["candidate_head_sha"],
            name=trusted_policy["checks"]["targeted_verification"],
        )
        ci = reader.check_record(
            repository=candidate["repository"],
            head_sha=candidate["candidate_head_sha"],
            name=trusted_policy["checks"]["continuous_integration"],
        )
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
        output = receipt_output.expanduser().resolve()
        private = runtime_root()
        if output != private and private not in output.parents:
            raise ContractError("Validation receipt output must stay private")
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


def _authority_costs(
    authorities: list[Any],
) -> dict[str, EndpointCost]:
    return {
        authority.authority_id: validation_authority_cost(authority)
        for authority in authorities
    }
