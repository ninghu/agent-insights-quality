from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from agent_insights_quality.artifact_io import (
    content_hash,
    read_json_object,
    verified_hash,
    write_bytes_atomic,
)
from agent_insights_quality.contracts import (
    ContractError,
    ROOT,
    SCHEMAS,
    load_agent_manifests,
    load_data,
    load_scenario_catalog,
    validate_daily_plan_semantics,
    validate_instance,
)
from agent_insights_quality.finalizer import (
    build_failure_report,
    create_failure_send_request,
    render_failure_email_html,
    write_daily_artifacts_to_reports_root,
)
from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.judging import (
    export_judge_package,
    validate_evidence_bundle,
    validate_judge_package,
)
from agent_insights_quality.planning import (
    generate_daily_plan,
    render_plan_markdown,
    serialize_plan,
)
from agent_insights_quality.public_safety import require_public_artifact_safe
from agent_insights_quality.reporting import resolve_recipient
from agent_insights_quality.runtime.adapters import (
    DEFAULT_RUNTIME_ADAPTER,
    load_runtime_hooks,
)
from agent_insights_quality.runtime.azure import AzureCli, select_azure_context
from agent_insights_quality.runtime.config import RuntimeConfig
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.orchestrator import (
    PlanInput,
    ProductionOrchestrator,
    RunState,
    VersionWork,
)
from agent_insights_quality.runtime.receipts import (
    ensure_public_safe,
    read_receipt,
    write_receipt,
)


_SHA256 = "sha256:"
_WORKFLOW_STAGES = (
    "primary_judgment",
    "blinded_verification",
    "deterministic_scoring_mapping",
    "memory_reconciliation",
    "ado_candidate_only",
    "canonical_report_finalize",
    "one_message_email",
    "generated_only_pr",
    "reviewed_cleanup",
)


def _immutable_bytes(path: Path, content: bytes, label: str) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise ContractError(f"{label} is immutable and differs from the reviewed content")
        return
    write_bytes_atomic(path, content)


def _immutable_json(path: Path, value: Mapping[str, Any], label: str) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True).encode("ascii") + b"\n"
    _immutable_bytes(path, rendered, label)


def _artifact_directory(output_root: Path, plan: Mapping[str, Any]) -> Path:
    relative = PurePosixPath(str(plan["artifact_directory"]))
    if not relative.parts or relative.parts[0] != "reports":
        raise ContractError("Daily plan artifact directory must remain under reports/")
    return output_root.joinpath(*relative.parts[1:])


def ensure_daily_plan(
    report_date: date,
    output_root: Path,
    *,
    rerun: int = 0,
) -> tuple[dict[str, Any], Path]:
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    plan = generate_daily_plan(
        report_date,
        agents=agents,
        catalog=catalog,
        rerun=rerun,
    )
    validate_instance(plan, SCHEMAS / "daily-plan.schema.json", "daily plan")
    validate_daily_plan_semantics(plan, agents, catalog, "daily plan")
    target = _artifact_directory(output_root, plan)
    json_path = target / "plan.json"
    markdown_path = target / "plan.md"
    validate_generated_paths(
        [
            f"{plan['artifact_directory']}/plan.json",
            f"{plan['artifact_directory']}/plan.md",
        ]
    )
    _immutable_bytes(json_path, serialize_plan(plan), "Daily plan JSON")
    _immutable_bytes(
        markdown_path,
        render_plan_markdown(plan, catalog).encode("ascii"),
        "Daily plan Markdown",
    )
    return plan, target


def _resource_leaf(
    resource_id: str | None,
    *,
    subscription_id: str,
    resource_group: str,
    provider_type: str,
    label: str,
) -> str:
    if resource_id is None:
        raise RuntimeFailure(
            "missing_runtime_configuration",
            f"{label} resource ID is required for daily deployment.",
        )
    expected = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/{provider_type}/"
    )
    if not resource_id.casefold().startswith(expected.casefold()):
        raise RuntimeFailure(
            "invalid_runtime_configuration",
            f"{label} resource ID is outside the exact configured parent.",
        )
    leaf = resource_id[len(expected) :]
    if not leaf or "/" in leaf:
        raise RuntimeFailure(
            "invalid_runtime_configuration",
            f"{label} resource ID does not identify one exact resource.",
        )
    return leaf


def _resource_subscription_id(resource_id: str | None) -> str:
    if resource_id is None:
        raise RuntimeFailure(
            "missing_runtime_configuration",
            "Application Insights resource ID is required for daily deployment.",
        )
    parts = resource_id.strip("/").split("/")
    normalized = [part.casefold() for part in parts]
    try:
        subscription = parts[normalized.index("subscriptions") + 1]
    except (ValueError, IndexError) as error:
        raise RuntimeFailure(
            "invalid_runtime_configuration",
            "Application Insights resource ID has no subscription identity.",
        ) from error
    return subscription


def qualification_project_parameters(
    plan: Mapping[str, Any],
    config: RuntimeConfig,
    *,
    subscription_id: str,
) -> dict[str, str]:
    azure = config.azure
    if not azure.resource_group or not azure.account_name:
        raise RuntimeFailure(
            "missing_runtime_configuration",
            "Daily deployment requires an exact resource group and Foundry account.",
        )
    registry_name = azure.container_registry_name
    if registry_name is None:
        raise RuntimeFailure(
            "missing_runtime_configuration",
            "AIQ_CONTAINER_REGISTRY_NAME is required for daily deployment.",
        )
    application_insights_name = _resource_leaf(
        azure.application_insights_resource_id,
        subscription_id=subscription_id,
        resource_group=azure.resource_group,
        provider_type="Microsoft.Insights/components",
        label="Application Insights",
    )
    project = plan.get("project")
    if not isinstance(project, Mapping):
        raise RuntimeFailure("invalid_plan", "Daily plan project contract is missing.")
    return {
        "accountName": azure.account_name,
        "projectName": str(project["name"]),
        "applicationInsightsName": application_insights_name,
        "registryName": registry_name,
        "reportDate": str(plan["report_date"]),
        "expiresOn": str(project["expires_on"]),
        "automationOwner": config.automation_owner,
        "catalogVersion": str(plan["catalog_hash"]),
        "connectionNameSuffix": str(project["name"]),
    }


def deploy_qualification_project(
    plan: Mapping[str, Any],
    config: RuntimeConfig,
    cli: AzureCli,
) -> dict[str, Any]:
    context = select_azure_context(cli, config.azure)
    parameters = qualification_project_parameters(
        plan,
        config,
        subscription_id=context.subscription_id,
    )
    resource_group = config.azure.resource_group
    if resource_group is None:
        raise RuntimeFailure(
            "missing_runtime_configuration",
            "Daily deployment requires an exact resource group.",
        )
    arguments = [
        "deployment",
        "group",
        "create",
        "--name",
        str(plan["project"]["name"]),
        "--resource-group",
        resource_group,
        "--template-file",
        str(ROOT / "infra" / "modules" / "qualification-project.bicep"),
        "--parameters",
        *(f"{name}={value}" for name, value in parameters.items()),
    ]
    result = cli.json(arguments, timeout=1800)
    if not isinstance(result, Mapping):
        raise RuntimeFailure(
            "qualification_deployment_failed",
            "Qualification project deployment returned an invalid response.",
        )
    properties = result.get("properties")
    state = (
        str(properties.get("provisioningState") or "")
        if isinstance(properties, Mapping)
        else ""
    )
    if state.casefold() != "succeeded":
        raise RuntimeFailure(
            "qualification_deployment_failed",
            "Qualification project deployment did not reach Succeeded.",
        )
    return {
        "schema_version": "1.0.0",
        "plan_id": plan["plan_id"],
        "plan_reference": plan["plan_digest"],
        "parameter_reference": content_hash(parameters),
        "status": "succeeded",
    }


def ensure_qualification_deployment(
    plan: Mapping[str, Any],
    config: RuntimeConfig,
    cli: AzureCli,
    receipt_path: Path,
    *,
    propagation_wait_seconds: float = 900,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if (
        isinstance(propagation_wait_seconds, bool)
        or not isinstance(propagation_wait_seconds, (int, float))
        or propagation_wait_seconds < 0
        or propagation_wait_seconds > 1800
    ):
        raise RuntimeFailure(
            "invalid_propagation_wait",
            "Post-deployment propagation wait must be between 0 and 1800 seconds.",
        )
    receipt: dict[str, Any] | None = None
    if receipt_path.exists():
        receipt = read_receipt(receipt_path)
        configured_subscription = _resource_subscription_id(
            config.azure.application_insights_resource_id
        )
        expected_parameter_reference = content_hash(
            qualification_project_parameters(
                plan,
                config,
                subscription_id=configured_subscription,
            )
        )
        if (
            receipt.get("schema_version") != "1.0.0"
            or receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("plan_reference") != plan["plan_digest"]
            or receipt.get("parameter_reference") != expected_parameter_reference
            or receipt.get("status") not in {"deployed", "succeeded"}
        ):
            raise RuntimeFailure(
                "deployment_receipt_mismatch",
                "Daily deployment receipt does not match the immutable plan.",
            )
        if receipt["status"] == "succeeded":
            return receipt
        if receipt.get("propagation_wait_seconds") != propagation_wait_seconds:
            raise RuntimeFailure(
                "deployment_receipt_mismatch",
                "Pending propagation receipt uses a different bounded wait.",
            )
    if receipt is None:
        receipt = deploy_qualification_project(plan, config, cli)
        receipt["status"] = "deployed"
        receipt["propagation_wait_seconds"] = propagation_wait_seconds
        ensure_public_safe(receipt)
        write_receipt(receipt_path, receipt)
    sleeper(propagation_wait_seconds)
    receipt["status"] = "succeeded"
    ensure_public_safe(receipt)
    write_receipt(receipt_path, receipt)
    return receipt


def _final_work_by_scenario(
    plan: PlanInput,
    payload: Mapping[str, Any],
) -> dict[str, VersionWork]:
    work_by_identity = {
        (
            work.agent_id,
            work.version_reference,
            work.run_id,
            work.phase,
        ): work
        for versions in plan.agents.values()
        for work in versions
    }
    selected: dict[str, VersionWork] = {}
    for assignment in payload["assignments"]:
        final = assignment["version_sequence"][-1]
        key = (
            assignment["agent_id"],
            final["digest"],
            assignment["run_id"],
            final["phase"],
        )
        work = work_by_identity.get(key)
        if work is None:
            raise RuntimeFailure(
                "evidence_reference_incomplete",
                "The final planned version has no runtime work item.",
            )
        selected[assignment["scenario_id"]] = work
    return selected


def _evidence_result(
    hooks: Any,
    state: RunState,
    work: VersionWork,
) -> Mapping[str, Any]:
    key = f"{work.key}:evidence"
    checkpoint = state.checkpoints.get(key)
    if checkpoint is None:
        raise RuntimeFailure(
            "evidence_reference_incomplete",
            "A selected final version has no evidence checkpoint.",
        )
    result = hooks.recover(key, checkpoint)
    references = result.get("evidence_references")
    if (
        not isinstance(references, list)
        or result.get("evidence_count") != len(work.assignments)
        or len(references) != len(work.assignments)
        or not all(isinstance(item, str) and item.startswith(_SHA256) for item in references)
    ):
        raise RuntimeFailure(
            "evidence_reference_incomplete",
            "A selected final version has incomplete evidence references.",
        )
    return result


def _load_evidence_bundle(
    hooks: Any,
    work: VersionWork,
    scenario_id: str,
    reference: str,
) -> dict[str, Any]:
    loader = getattr(hooks, "load_evidence_bundle", None)
    if not callable(loader):
        raise RuntimeFailure(
            "runtime_adapter_unavailable",
            "The live adapter cannot materialize completed evidence for Copilot judgment.",
        )
    bundle = loader(work, scenario_id, reference)
    if not isinstance(bundle, dict):
        raise RuntimeFailure(
            "evidence_reference_incomplete",
            "The live adapter returned an invalid evidence bundle.",
        )
    validate_evidence_bundle(bundle)
    return bundle


def _workflow_contract() -> dict[str, Any]:
    return {
        "stages": [
            {
                "name": "primary_judgment",
                "status": "pending",
                "commands": ["judge-package-export", "judge-package-import"],
                "scope": "every_primary_judgment_target",
            },
            {
                "name": "blinded_verification",
                "status": "conditional",
                "commands": ["verifier-export", "verifier-import"],
                "scope": "eligible_candidates_only",
            },
            {
                "name": "deterministic_scoring_mapping",
                "status": "pending",
                "commands": ["score"],
                "scope": "complete_evidence_and_primary_judgments",
            },
            {
                "name": "memory_reconciliation",
                "status": "pending",
                "commands": ["memory-reconcile"],
                "scope": "complete_canonical_report_only",
            },
            {
                "name": "ado_candidate_only",
                "status": "pending",
                "commands": ["ado-dry-run"],
                "scope": "confirmed_agent_insights_defects",
            },
            {
                "name": "canonical_report_finalize",
                "status": "pending",
                "commands": ["render-report", "finalize"],
                "scope": "one_canonical_report",
            },
            {
                "name": "one_message_email",
                "status": "pending",
                "commands": ["email-receipt-import"],
                "scope": "one_content_digest",
            },
            {
                "name": "generated_only_pr",
                "status": "pending",
                "commands": ["validate-generated-paths"],
                "scope": "aiq-daily_branch",
            },
            {
                "name": "reviewed_cleanup",
                "status": "conditional",
                "commands": ["cleanup"],
                "scope": "exact_expired_owned_resources",
            },
        ],
        "verifier_eligibility": {
            "candidate_only": True,
            "primary_confidence_minimum": 0.95,
            "primary_verdicts": ["partially_useful", "incorrect_noise"],
            "deterministic_checks_required": True,
            "provenance_checks_required": True,
            "reproducible_occurrence_required": True,
            "agent_insights_ownership_required": True,
        },
        "ado": {
            "mode": "candidate-only",
            "auto_apply": False,
            "write_requests_allowed": False,
        },
        "email": {
            "logical_message_limit": 1,
            "receipt_required": True,
            "stop_after_first_confirmed_success": True,
        },
        "generated_pr": {
            "branch_prefix": "aiq-daily/",
            "generated_paths_only": True,
        },
        "cleanup": {
            "dry_run_first": True,
            "execute_requires_review": True,
        },
    }


def build_daily_status(
    plan_payload: Mapping[str, Any],
    plan: PlanInput,
    state: RunState,
    hooks: Any,
    package_root: Path,
) -> dict[str, Any]:
    if state.status != "succeeded" or state.phase != "complete":
        raise RuntimeFailure(
            "evidence_reference_incomplete",
            "Daily judgment handoff requires a successful evidence-complete runtime state.",
        )
    project_key = f"{plan.plan_id}:project"
    project_checkpoint = state.checkpoints.get(project_key)
    if project_checkpoint is None:
        raise RuntimeFailure(
            "evidence_reference_incomplete",
            "A successful runtime state has no validated project checkpoint.",
        )
    project_result = hooks.recover(project_key, project_checkpoint)
    if not isinstance(project_result, Mapping):
        raise RuntimeFailure(
            "evidence_reference_incomplete",
            "The validated project checkpoint returned an invalid receipt.",
        )
    final_work = _final_work_by_scenario(plan, plan_payload)
    results_by_work: dict[str, Mapping[str, Any]] = {}
    evidence = []
    primary_target_references: set[str] = set()
    for assignment in plan_payload["assignments"]:
        scenario_id = str(assignment["scenario_id"])
        work = final_work[scenario_id]
        if work.key not in results_by_work:
            results_by_work[work.key] = _evidence_result(hooks, state, work)
        result = results_by_work[work.key]
        assignment_ids = [
            str(item["scenario_id"])
            for item in work.assignments
        ]
        try:
            reference = str(
                result["evidence_references"][assignment_ids.index(scenario_id)]
            )
        except (KeyError, ValueError, IndexError, TypeError) as error:
            raise RuntimeFailure(
                "evidence_reference_incomplete",
                "A selected scenario has no exact final evidence reference.",
            ) from error
        bundle = _load_evidence_bundle(hooks, work, scenario_id, reference)
        final_version = assignment["version_sequence"][-1]
        if (
            bundle["plan_id"] != plan.plan_id
            or bundle["scenario"]["id"] != scenario_id
            or bundle["scenario"]["version"] != assignment["scenario_version"]
            or bundle["agent"]["id"] != assignment["agent_id"]
            or bundle["agent"]["name"] != assignment["agent_name"]
            or bundle["agent"]["type"] != assignment["agent_type"]
            or bundle["agent"]["version_digest"] != final_version["digest"]
            or bundle["run"]["run_id"] != assignment["run_id"]
            or bundle["run"]["engine_build"] != plan.engine_build
            or bundle["run"]["generator_model"] != plan.generator_model
            or bundle["version_sequence"]["phase"]
            != final_version["phase"]
            or bundle["version_sequence"]["run_id"] != assignment["run_id"]
            or bundle["version_sequence"]["version_digest"] != final_version["digest"]
            or any(
                trace["project_reference"]
                != plan_payload["project"]["resource_reference"]
                or trace["agent_id"] != assignment["agent_id"]
                or trace["version_digest"] != final_version["digest"]
                for trace in bundle["trace_evidence"]
            )
        ):
            raise RuntimeFailure(
                "evidence_reference_incomplete",
                "A completed evidence bundle does not match its final plan assignment.",
            )
        package = export_judge_package(bundle, "primary")
        package_path = package_root / f"{scenario_id}-primary-package.json"
        _immutable_json(package_path, package, "Primary judgment package")
        insight_ids: list[str | None] = (
            [str(item["id"]) for item in bundle["insights"]]
            if bundle["insights"]
            else [None]
        )
        targets = []
        for insight_id in insight_ids:
            insight_reference = (
                content_hash({"insight_id": insight_id})
                if insight_id is not None
                else None
            )
            target_reference = content_hash(
                {
                    "plan_id": plan.plan_id,
                    "scenario_id": scenario_id,
                    "bundle_hash": bundle["bundle_hash"],
                    "insight_reference": insight_reference,
                    "judge_role": "primary",
                }
            )
            if target_reference in primary_target_references:
                raise RuntimeFailure(
                    "judgment_target_conflict",
                    "Primary judgment targets are not unique.",
                )
            primary_target_references.add(target_reference)
            targets.append(
                {
                    "insight_reference": insight_reference,
                    "judgment_target_reference": target_reference,
                }
            )
        evidence.append(
            {
                "scenario_id": scenario_id,
                "agent_id": assignment["agent_id"],
                "run_id": assignment["run_id"],
                "phase": assignment["version_sequence"][-1]["phase"],
                "artifact_reference": reference,
                "bundle_hash": bundle["bundle_hash"],
                "primary_package_reference": package["package_hash"],
                "validation_targets": list(assignment["expected"]["validation_targets"]),
                "primary_judgment_targets": targets,
            }
        )
    status: dict[str, Any] = {
        "schema_version": "1.0.0",
        "status_id": plan.plan_id,
        "status_hash": "",
        "plan_id": plan.plan_id,
        "plan_reference": plan.reference,
        "report_date": plan.report_date,
        "generated_at": f"{plan.report_date}T00:00:00Z",
        "stage": "evidence_complete",
        "runtime": {
            "status": "succeeded",
            "state_reference": content_hash(state.public_dict()),
            "attempt": state.attempt,
        },
        "evidence": evidence,
        "primary_package_count": len(evidence),
        "primary_judgment_target_count": len(primary_target_references),
        "workflow": _workflow_contract(),
    }
    status["status_hash"] = content_hash(
        {key: value for key, value in status.items() if key != "status_hash"}
    )
    validate_daily_status(status, plan_payload)
    return status


def validate_daily_status(
    status: dict[str, Any],
    plan: Mapping[str, Any],
) -> None:
    validate_instance(status, SCHEMAS / "daily-status.schema.json", "daily status")
    verified_hash(status, "status_hash", "daily status")
    if (
        status["status_id"] != plan["plan_id"]
        or status["generated_at"] != f"{plan['report_date']}T00:00:00Z"
        or status["plan_id"] != plan["plan_id"]
        or status["plan_reference"] != plan["plan_digest"]
        or status["report_date"] != plan["report_date"]
    ):
        raise ContractError("daily status: immutable plan identity mismatch")
    selected = [assignment["scenario_id"] for assignment in plan["assignments"]]
    observed = [item["scenario_id"] for item in status["evidence"]]
    if observed != selected or len(observed) != len(set(observed)):
        raise ContractError(
            "daily status: evidence references must cover every selected scenario exactly once"
        )
    for assignment, evidence in zip(plan["assignments"], status["evidence"], strict=True):
        final = assignment["version_sequence"][-1]
        if (
            evidence["agent_id"] != assignment["agent_id"]
            or evidence["run_id"] != assignment["run_id"]
            or evidence["phase"] != final["phase"]
            or evidence["validation_targets"]
            != assignment["expected"]["validation_targets"]
        ):
            raise ContractError(
                "daily status: evidence identity differs from its immutable plan assignment"
            )
    if status["primary_package_count"] != len(observed):
        raise ContractError("daily status: primary package count is incomplete")
    target_references = [
        target["judgment_target_reference"]
        for item in status["evidence"]
        for target in item["primary_judgment_targets"]
    ]
    if (
        status["primary_judgment_target_count"] != len(target_references)
        or len(target_references) != len(set(target_references))
    ):
        raise ContractError("daily status: primary judgment targets are incomplete or duplicated")
    stages = [item["name"] for item in status["workflow"]["stages"]]
    if tuple(stages) != _WORKFLOW_STAGES:
        raise ContractError("daily status: workflow stages changed or are out of order")
    if status["workflow"] != _workflow_contract():
        raise ContractError("daily status: workflow contract was modified")
    require_public_artifact_safe(status, "Daily status")


def validate_daily_status_packages(
    status: dict[str, Any],
    package_root: Path,
) -> None:
    for evidence in status["evidence"]:
        scenario_id = evidence["scenario_id"]
        package = read_json_object(
            package_root / f"{scenario_id}-primary-package.json",
            "primary judgment package",
        )
        validate_judge_package(package)
        bundle = package["evidence"]
        rendered_bundle = (
            json.dumps(bundle, indent=2, sort_keys=True).encode("ascii") + b"\n"
        )
        artifact_reference = (
            "sha256:" + hashlib.sha256(rendered_bundle).hexdigest()
        )
        expected_targets = []
        insight_ids: list[str | None] = (
            [str(item["id"]) for item in bundle["insights"]]
            if bundle["insights"]
            else [None]
        )
        for insight_id in insight_ids:
            insight_reference = (
                content_hash({"insight_id": insight_id})
                if insight_id is not None
                else None
            )
            expected_targets.append(
                {
                    "insight_reference": insight_reference,
                    "judgment_target_reference": content_hash(
                        {
                            "plan_id": status["plan_id"],
                            "scenario_id": scenario_id,
                            "bundle_hash": bundle["bundle_hash"],
                            "insight_reference": insight_reference,
                            "judge_role": "primary",
                        }
                    ),
                }
            )
        if (
            package["package_hash"] != evidence["primary_package_reference"]
            or bundle["bundle_hash"] != evidence["bundle_hash"]
            or bundle["scenario"]["id"] != scenario_id
            or artifact_reference != evidence["artifact_reference"]
            or expected_targets != evidence["primary_judgment_targets"]
        ):
            raise ContractError(
                "daily status: private primary package does not match its evidence entry"
            )


def _completed_scenarios(
    plan_payload: Mapping[str, Any],
    plan: PlanInput,
    state: Mapping[str, Any],
) -> list[str]:
    checkpoints = state.get("checkpoints")
    if not isinstance(checkpoints, Mapping):
        return []
    final_work = _final_work_by_scenario(plan, plan_payload)
    return [
        assignment["scenario_id"]
        for assignment in plan_payload["assignments"]
        if f"{final_work[assignment['scenario_id']].key}:evidence" in checkpoints
    ]


def _failure_details(
    error: Exception,
    plan_payload: Mapping[str, Any],
    plan: PlanInput,
    state_directory: Path,
) -> dict[str, Any]:
    state_path = state_directory / "runtime-state.json"
    state: dict[str, Any] = {}
    if state_path.exists():
        state = read_receipt(state_path)
    code = error.code if isinstance(error, RuntimeFailure) else "daily_contract_failure"
    error_phase = (
        error.details.get("phase")
        if isinstance(error, RuntimeFailure)
        else None
    )
    phase = (
        str(state.get("failed_phase") or error_phase or state.get("phase") or "")
        or ("deployment" if code == "qualification_deployment_failed" else "preflight")
    )
    completed = _completed_scenarios(plan_payload, plan, state)
    completed_set = set(completed)
    affected_agents = sorted(
        {
            assignment["agent_id"]
            for assignment in plan_payload["assignments"]
            if assignment["scenario_id"] not in completed_set
        }
    )
    checkpoints = state.get("checkpoints")
    checkpoint_keys = list(checkpoints) if isinstance(checkpoints, Mapping) else []
    confirmed_stage = "plan_validated"
    deployment_path = state_directory / "qualification-deployment.json"
    if deployment_path.exists():
        deployment = read_receipt(deployment_path)
        confirmed_stage = (
            "authorization_propagation"
            if deployment.get("status") == "succeeded"
            else "bicep_deployment"
        )
    if "preflight" in checkpoint_keys:
        confirmed_stage = "preflight"
    if f"{plan.plan_id}:project" in checkpoint_keys:
        confirmed_stage = "project_validation"
    for suffix, stage in (
        (":deploy", "deployment"),
        (":invoke", "endpoint_traffic"),
        (":ingestion", "trace_ingestion"),
        (":insights", "agent_insights"),
        (":evidence", "evidence"),
    ):
        if any(key.endswith(suffix) for key in checkpoint_keys):
            confirmed_stage = stage
    failure = state.get("failure")
    diagnostics_reference = (
        failure.get("artifact_reference")
        if isinstance(failure, Mapping)
        and isinstance(failure.get("artifact_reference"), str)
        else content_hash(
            {
                "plan_id": plan.plan_id,
                "phase": phase,
                "code": code,
                "attempt": state.get("attempt", 1),
            }
        )
    )
    return {
        "failed_phase": phase,
        "last_confirmed_stage": confirmed_stage,
        "reason": f"Daily qualification failed closed with {code}.",
        "affected_agents": affected_agents,
        "diagnostics_reference": diagnostics_reference,
        "next_action": (
            "Review the retained private diagnostics and start an explicit rerun with the "
            "next immutable rerun suffix."
        ),
        "completed_scenarios": [],
        "evidence_completed_scenarios": completed,
    }


def finalize_operational_failure(
    error: Exception,
    plan_payload: dict[str, Any],
    plan: PlanInput,
    *,
    output_root: Path,
    state_directory: Path,
) -> Path:
    failure_path = state_directory / "operational-failure.json"
    if failure_path.exists():
        failure = read_json_object(failure_path, "operational failure")
    else:
        failure = _failure_details(
            error,
            plan_payload,
            plan,
            state_directory,
        )
        _immutable_json(failure_path, failure, "Operational failure")
    report = build_failure_report(
        plan_payload,
        failure,
        generated_at=f"{plan.report_date}T00:00:00Z",
    )
    body = render_failure_email_html(report)
    request = create_failure_send_request(
        report,
        resolve_recipient(load_data(ROOT / "config" / "reporting.yaml")),
    )
    report["delivery"]["request_reference"] = request["request_hash"]
    request_path = state_directory / "email-send-request.json"
    _immutable_json(request_path, request, "Operational failure email request")
    write_daily_artifacts_to_reports_root(
        output_root,
        plan_payload,
        report,
        failure_email=body,
    )
    return request_path


def run_daily(
    report_date: date,
    *,
    output_root: Path = ROOT / "reports",
    state_root: Path = ROOT / ".aiq-runtime",
    rerun: int = 0,
    max_parallel_agents: int = 5,
    environment: Mapping[str, str] | None = None,
    cli_factory: Callable[[], AzureCli] = AzureCli,
    hooks_factory: Callable[[RuntimeConfig], Any] = load_runtime_hooks,
    propagation_wait_seconds: float = 900,
    propagation_sleeper: Callable[[float], None] = time.sleep,
) -> Path:
    base_directory = output_root / "daily" / f"{report_date:%Y}" / f"{report_date:%m}" / f"{report_date:%d}"
    if rerun == 0 and any(
        (base_directory / filename).exists()
        for filename in (
            "readiness-failure.json",
            "readiness-failure.md",
            "failure-email.html",
            "email-send-request.json",
            "email-receipt.json",
            "email-handoff.json",
            "report.json",
            "report.md",
        )
    ):
        raise RuntimeFailure(
            "readiness_failure_already_finalized",
            "This date already has a finalized readiness failure; use an explicit rerun suffix.",
        )
    plan_payload, artifact_directory = ensure_daily_plan(
        report_date,
        output_root,
        rerun=rerun,
    )
    plan = PlanInput.from_daily_plan(plan_payload)
    state_directory = state_root / "daily" / plan.plan_id
    state_path = state_directory / "runtime-state.json"
    failure_path = state_directory / "operational-failure.json"
    status_path = artifact_directory / "daily-status.json"
    if status_path.exists():
        try:
            status = read_json_object(status_path, "daily status")
            validate_daily_status(status, plan_payload)
            validate_daily_status_packages(
                status,
                state_directory / "judge-packages",
            )
            return status_path
        except (ContractError, RuntimeFailure, OSError) as error:
            state_directory.mkdir(parents=True, exist_ok=True)
            finalize_operational_failure(
                error,
                plan_payload,
                plan,
                output_root=output_root,
                state_directory=state_directory,
            )
            raise RuntimeFailure(
                "daily_qualification_failed",
                "Daily qualification is INCONCLUSIVE; the email-required handoff was "
                "finalized in private state.",
            ) from error
    published_report = artifact_directory / "report.json"
    if failure_path.exists():
        prior_state = read_receipt(state_path) if state_path.exists() else {}
        prior_failure = read_json_object(failure_path, "operational failure")
        prior_error = RuntimeFailure(
            (
                str(prior_state["failure"]["code"])
                if isinstance(prior_state.get("failure"), Mapping)
                else "daily_qualification_failed"
            ),
            str(prior_failure["reason"]),
        )
        finalize_operational_failure(
            prior_error,
            plan_payload,
            plan,
            output_root=output_root,
            state_directory=state_directory,
        )
        raise RuntimeFailure(
            "daily_run_already_finalized",
            "This immutable plan already has a finalized failure; use an explicit rerun suffix.",
        )
    if published_report.exists():
        report = read_json_object(published_report, "published daily report")
        if report.get("status") == "INCONCLUSIVE" and report.get("failure"):
            raise RuntimeFailure(
                "daily_run_already_finalized",
                "This immutable plan already has a finalized failure; use an explicit rerun suffix.",
            )
        raise RuntimeFailure(
            "daily_run_already_finalized",
            "This immutable plan already has a canonical report.",
        )

    state_directory.mkdir(parents=True, exist_ok=True)
    request_path = state_directory / "email-send-request.json"
    if request_path.exists():
        prior_state = read_receipt(state_path) if state_path.exists() else {}
        prior_error = RuntimeFailure(
            str(
                prior_state.get("failure", {}).get("code")
                if isinstance(prior_state.get("failure"), Mapping)
                else "daily_qualification_failed"
            ),
            "A prior operational failure is awaiting finalization.",
        )
        finalize_operational_failure(
            prior_error,
            plan_payload,
            plan,
            output_root=output_root,
            state_directory=state_directory,
        )
        raise RuntimeFailure(
            "daily_run_already_finalized",
            "This immutable plan already has a finalized failure; use an explicit rerun suffix.",
        )

    try:
        operational_stage = "runtime_configuration"
        config = RuntimeConfig.from_env(environment)
        config = replace(
            config,
            adapter=DEFAULT_RUNTIME_ADAPTER,
            azure=replace(
                config.azure,
                project_name=plan.project_name,
                project_endpoint=None,
            ),
        )
        cli = cli_factory()
        operational_stage = "qualification_provisioning"
        ensure_qualification_deployment(
            plan_payload,
            config,
            cli,
            state_directory / "qualification-deployment.json",
            propagation_wait_seconds=propagation_wait_seconds,
            sleeper=propagation_sleeper,
        )
        operational_stage = "runtime_preflight"
        hooks = hooks_factory(config)
        prior_succeeded = False
        if state_path.exists():
            prior_succeeded = read_receipt(state_path).get("status") == "succeeded"
        state = ProductionOrchestrator(
            hooks,
            state_path,
            max_parallel_agents=max_parallel_agents,
        ).run(
            plan,
            resume=state_path.exists(),
        )
        operational_stage = "evidence_handoff"
        if prior_succeeded:
            hooks.preflight(plan, dry_run=False)
        status = build_daily_status(
            plan_payload,
            plan,
            state,
            hooks,
            state_directory / "judge-packages",
        )
        validate_daily_status_packages(
            status,
            state_directory / "judge-packages",
        )
        validate_generated_paths([f"{plan_payload['artifact_directory']}/daily-status.json"])
        _immutable_json(status_path, status, "Daily status")
        return status_path
    except (ContractError, RuntimeFailure, OSError) as error:
        if isinstance(error, RuntimeFailure):
            error.details.setdefault("phase", operational_stage)
        finalize_operational_failure(
            error,
            plan_payload,
            plan,
            output_root=output_root,
            state_directory=state_directory,
        )
        raise RuntimeFailure(
            "daily_qualification_failed",
            "Daily qualification is INCONCLUSIVE; the email-required handoff was "
            "finalized in private state.",
            {"state_reference_count": 1 if state_path.exists() else 0},
        ) from error
    except Exception as error:
        unexpected = RuntimeFailure(
            "unexpected_daily_failure",
            "Daily qualification encountered an unexpected operational failure.",
            {"phase": operational_stage},
        )
        finalize_operational_failure(
            unexpected,
            plan_payload,
            plan,
            output_root=output_root,
            state_directory=state_directory,
        )
        raise RuntimeFailure(
            "daily_qualification_failed",
            "Daily qualification is INCONCLUSIVE; the email-required handoff was "
            "finalized in private state.",
            {"state_reference_count": 1 if state_path.exists() else 0},
        ) from error
