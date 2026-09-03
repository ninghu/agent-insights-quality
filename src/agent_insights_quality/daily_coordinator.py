from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from agent_insights_quality.assessment import (
    _baseline_evidence_complete,
    _endpoint_evidence_complete,
    _issue_activation_complete,
    load_assessments,
    load_baseline_assessments,
    rehydrate_packages,
)
from agent_insights_quality.automation_policy import (
    AutomationPolicy,
    load_automation_policy,
)
from agent_insights_quality.catalogs import (
    agent_model_contract,
    catalog_hashes,
    load_catalogs,
)
from agent_insights_quality.daily_lifecycle import (
    AGENT_ORDER,
    DailyLifecycle,
    DailyLock,
    DailyRecord,
    artifact_reference,
    daily_runtime_root,
)
from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.github_preview import validate_preview_publication
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.models import (
    AgentResult,
    InsightEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.provisioning import provision_profile
from agent_insights_quality.registry import (
    ENVIRONMENT_ID,
    PROFILE_LOCATION,
    TELEMETRY_RESOURCE_SET,
    load_registry,
    sync_registry,
    version_entry,
)
from agent_insights_quality.run_manifest import (
    OFFICIAL_DELIVERY,
    TEST_EMAIL_ONLY_DELIVERY,
    build_manifest,
    run_id,
    validate_manifest,
)
from agent_insights_quality.runner import execute_agent
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    content_hash,
    file_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_approved import (
    fetch_approved_record_for_checkout,
    validate_approval_binding,
)
from agent_insights_quality.validation_blob import AzureValidationBlobStore
from agent_insights_quality.validation_credentials import local_azure_operator
from agent_insights_quality.validation_local import current_clean_commit
from agent_insights_quality.validation_manifest import current_validation_digest
from agent_insights_quality.work_items import load_quality_work_items

REPOSITORY = "ninghu/agent-insights-quality"
_RECEIPT_SCHEMA = ROOT / "schemas" / "daily-agent-receipt.schema.json"
_DISPLAY_NAMES = {
    "weather-agent": "Weather",
    "healthcare-agent": "Healthcare",
    "finance-agent": "Finance",
    "travel-agent": "Travel",
    "support-ticket-agent": "Support",
}


def prepare_daily(
    *,
    report_date: date,
    work_items_path: Path,
    rerun: int = 0,
    test_run: bool = False,
    publish_preview: bool = False,
    base: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    if rerun < 0 or (test_run and rerun == 0):
        raise ContractError("Test runs require a nonzero --rerun identity")
    if publish_preview and not test_run:
        raise ContractError("GitHub preview publication requires --test-run")
    if not test_run and rerun:
        raise ContractError("Official Daily does not accept a manual rerun identity")
    moment_value = now()
    if moment_value.tzinfo is None:
        raise ContractError("Daily coordinator clock must include a timezone")
    if report_date != moment_value.astimezone(
        ZoneInfo("America/Los_Angeles")
    ).date():
        raise ContractError("Daily report date is not the current Pacific business date")
    private_root = (base or runtime_root()).resolve()
    work_items = load_quality_work_items(work_items_path, report_date=report_date)
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(report_date, agents, issues, hashes["issues"])
    policy = load_automation_policy()
    approval = _fetch_approved_record()
    checkout_commit_sha = current_clean_commit()
    validation_digest = current_validation_digest(agents, issues)
    approval = validate_approval_binding(
        approval,
        expected_checkout_commit_sha=checkout_commit_sha,
        expected_validation_digest=validation_digest,
    )
    resolved_work_items = work_items_path.resolve()
    if private_root != resolved_work_items and private_root not in resolved_work_items.parents:
        raise ContractError("Daily work-item snapshot must stay in the private runtime root")
    moment = moment_value.astimezone(UTC).isoformat()
    delivery_mode = TEST_EMAIL_ONLY_DELIVERY if test_run else OFFICIAL_DELIVERY
    initial = {
        "schema_version": "4.0.0",
        "kind": "daily-qualification-lifecycle",
        "snapshot_type": "event",
        "state": "LOCKED",
        "execution_id": uuid.uuid4().hex,
        "event_sequence": 0,
        "started_at": moment,
        "last_activity_at": moment,
        "previous_lifecycle_digest": None,
        "event_reference": None,
        "superseded_format_digest": None,
        "bindings": {
            "repository": REPOSITORY,
            "public_run_id": run_id(report_date, rerun),
            "report_date": report_date.isoformat(),
            "delivery_mode": delivery_mode,
            "publish_preview": publish_preview,
            "work_items": {
                "path": resolved_work_items.relative_to(private_root).as_posix(),
                "content_digest": content_hash(work_items),
                "closed_business_date": work_items["closed_business_date"],
            },
            "approval": approval,
            "catalog_hashes": hashes,
            "selection": selected,
            "policy": _policy_binding(policy),
            "registry": None,
            "run_contract_digest": None,
        },
        "artifacts": {
            "lane_receipts": {agent_name: None for agent_name in AGENT_ORDER},
            "manifest": None,
            "assessment_index": None,
            "improvement_input": None,
            "improvement_analysis": None,
            "final_report": None,
            "adx_publication_status": None,
            "email_request": None,
            "preview_publication": None,
            "send_claim": None,
            "email_receipt": None,
            "publication": None,
            "failure": None,
        },
        "lifecycle_digest": "",
    }
    lock = _coordinator_lock(private_root)
    with lock:
        active = DailyLifecycle(lock=lock, base=private_root).begin(initial)
    immutable_json(
        _run_root(active, private_root) / "work-items-reference.json",
        {
            "schema_version": "1.0.0",
            "run_id": active.value["bindings"]["public_run_id"],
            "report_date": report_date.isoformat(),
            "content_digest": content_hash(work_items),
        },
    )
    return _status(active, private_root)


def provision_daily(
    *,
    base: Path | None = None,
    profile_factory: Callable[[str], RuntimeProfile] | None = None,
    provisioner: Callable[..., dict[str, Any]] | None = None,
    registry_sync: Callable[[RuntimeProfile], None] | None = None,
) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    lock = _coordinator_lock(private_root)
    with lock:
        return _provision_daily_locked(
            private_root=private_root,
            lock=lock,
            profile_factory=profile_factory,
            provisioner=provisioner,
            registry_sync=registry_sync,
        )


def _provision_daily_locked(
    *,
    private_root: Path,
    lock: DailyLock,
    profile_factory: Callable[[str], RuntimeProfile] | None,
    provisioner: Callable[..., dict[str, Any]] | None,
    registry_sync: Callable[[RuntimeProfile], None] | None,
) -> dict[str, Any]:
    lifecycle = DailyLifecycle(lock=lock, base=private_root)
    active = lifecycle.read_active()
    if active.value["state"] != "LOCKED":
        raise ContractError("Daily lifecycle is not ready for provisioning")
    _assert_checkout_binding(active)
    agents, issues = load_catalogs()
    profile = (profile_factory or RuntimeProfile.from_env)("daily")
    profile.assert_insights_connection()
    profile.assert_test_agent_model(agent_model_contract(agents))
    (provisioner or provision_profile)(
        profile=profile,
        agents=agents,
        issues=issues,
        approved_digests=None,
    )
    (registry_sync or sync_registry)(profile)
    hashes = catalog_hashes(agents, issues)
    registry = load_registry(
        profile.registry_path,
        profile="daily",
        catalog_hashes=hashes,
    )
    test_region = profile.resolve_test_region()
    if registry["test_region"] != test_region:
        raise ContractError(
            "Live Daily Project region does not match the deployment registry"
        )
    bindings = active.value["bindings"]
    run_contract_digest = _daily_contract_digest(
        bindings=bindings,
        registry=registry,
        test_region=test_region,
        superseded_format_digest=active.value.get(
            "superseded_format_digest"
        ),
    )
    active = lifecycle.transition(
        active,
        next_state="PREPARED",
        binding_updates={
            "registry": {
                "content_digest": content_hash(registry),
                "project_name": profile.project_name,
                "test_region": test_region,
                "test_region_registry": registry["test_region"],
            },
            "run_contract_digest": run_contract_digest,
        },
    )
    return _status(active, private_root)


def run_daily_agent(
    agent_name: str,
    *,
    base: Path | None = None,
    profile_factory: Callable[[str], RuntimeProfile] | None = None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None = None,
) -> dict[str, Any]:
    if agent_name not in AGENT_ORDER:
        raise ContractError("Daily Agent lane is unknown")
    private_root = (base or runtime_root()).resolve()
    active = _read_active(private_root, allowed_states={"PREPARED", "TRAFFIC"})
    _assert_checkout_binding(active)
    lock = _coordinator_lock(private_root)
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=private_root)
        current = lifecycle.read_active()
        if current.value["state"] == "PREPARED":
            current = lifecycle.transition(current, next_state="TRAFFIC")
        elif current.value["state"] != "TRAFFIC":
            raise ContractError("Daily Agent lanes are not open")
        active = current
    lane_lock = DailyLock(_lane_root(active, private_root, agent_name) / "lane.lock")
    with lane_lock:
        receipt_path = _lane_receipt_path(active, private_root, agent_name)
        if receipt_path.is_file():
            receipt = _read_lane_receipt(receipt_path, active, agent_name)
            return _lane_result(receipt, resumed=True)
        capacity = _acquire_lane_capacity(active, private_root)
        try:
            return _run_daily_agent_locked(
                active=active,
                private_root=private_root,
                agent_name=agent_name,
                receipt_path=receipt_path,
                profile_factory=profile_factory,
                runtime_factory=runtime_factory,
            )
        finally:
            capacity.release()


def _run_daily_agent_locked(
    *,
    active: DailyRecord,
    private_root: Path,
    agent_name: str,
    receipt_path: Path,
    profile_factory: Callable[[str], RuntimeProfile] | None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None,
) -> dict[str, Any]:
    _daily_fence(active, private_root, allowed_states={"TRAFFIC"})
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    if hashes != active.value["bindings"]["catalog_hashes"]:
        raise ContractError("Daily Agent lane catalogs changed after preparation")
    policy = load_automation_policy()
    if _policy_binding(policy) != active.value["bindings"]["policy"]:
        raise ContractError("Daily Agent lane policy changed after preparation")
    profile = (profile_factory or RuntimeProfile.from_env)("daily")
    profile.assert_insights_connection()
    profile.assert_test_agent_model(agent_model_contract(agents))
    registry = load_registry(
        profile.registry_path,
        profile="daily",
        catalog_hashes=hashes,
    )
    expected_registry = active.value["bindings"]["registry"]
    if (
        expected_registry is None
        or content_hash(registry) != expected_registry["content_digest"]
    ):
        raise ContractError("Daily Agent lane registry changed after preparation")
    checkpoint_store = _checkpoint_store(active, private_root)
    runtime = _FencedRuntime(
        (runtime_factory or LiveRuntime)(profile),
        lambda: _daily_fence(active, private_root, allowed_states={"TRAFFIC"}),
    )
    lane_index = AGENT_ORDER.index(agent_name)
    start_delay = (
        0
        if checkpoint_store.has_progress(agent_name)
        else lane_index * policy.agent_start_stagger_seconds
    )
    result = execute_agent(
        agent_name=agent_name,
        agents=agents,
        issues=issues,
        selected=active.value["bindings"]["selection"],
        registry=registry,
        runtime=runtime,
        seed=_seed(active),
        lookback_hours=policy.insight_lookback_hours,
        clean_window_poll_seconds=policy.clean_window_poll_seconds,
        clean_window_ingestion_margin_seconds=(
            policy.clean_window_ingestion_margin_seconds
        ),
        clean_window_max_wait_seconds=policy.clean_window_max_wait_seconds,
        trace_assertion_stabilization_seconds=(
            policy.trace_assertion_stabilization_seconds
        ),
        insight_start_margin_seconds=policy.insight_start_margin_seconds,
        max_recovery_versions=policy.max_recovery_versions,
        checkpoint_store=checkpoint_store,
        start_delay_seconds=start_delay,
    )
    if checkpoint_store.has_unresolved_insight_state():
        raise ContractError(
            f"{agent_name} has unresolved Agent Insights state; resume its lane"
        )
    _daily_fence(active, private_root, allowed_states={"TRAFFIC"})
    receipt = _stamp_lane_receipt(active, agent_name, result)
    _validate_lane_receipt(receipt, active, agent_name)
    immutable_json(receipt_path, receipt)
    return _lane_result(receipt, resumed=False)


def compose_daily(
    *,
    base: Path | None = None,
    profile_factory: Callable[[str], RuntimeProfile] | None = None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None = None,
) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    lock = _coordinator_lock(private_root)
    with lock:
        return _compose_daily_locked(
            private_root=private_root,
            lock=lock,
            profile_factory=profile_factory,
            runtime_factory=runtime_factory,
        )


def _compose_daily_locked(
    *,
    private_root: Path,
    lock: DailyLock,
    profile_factory: Callable[[str], RuntimeProfile] | None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None,
) -> dict[str, Any]:
    lifecycle = DailyLifecycle(lock=lock, base=private_root)
    active = lifecycle.read_active()
    if active.value["state"] != "TRAFFIC":
        raise ContractError("Daily lifecycle is not ready for composition")
    _assert_checkout_binding(active)
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    profile = (profile_factory or RuntimeProfile.from_env)("daily")
    registry = load_registry(
        profile.registry_path,
        profile="daily",
        catalog_hashes=hashes,
    )
    receipts = [
        _read_lane_receipt(
            _lane_receipt_path(active, private_root, agent_name),
            active,
            agent_name,
        )
        for agent_name in AGENT_ORDER
    ]
    results = [_agent_result(receipt["result"]) for receipt in receipts]
    checkpoint_store = _checkpoint_store(active, private_root)
    _assert_unique_response_references(
        registry,
        results,
        checkpoint_store,
    )
    bindings = active.value["bindings"]
    policy = load_automation_policy()
    registry_binding = bindings["registry"]
    if (
        registry_binding is None
        or content_hash(registry) != registry_binding["content_digest"]
    ):
        raise ContractError("Daily registry changed before composition")
    manifest = build_manifest(
        report_date=date.fromisoformat(bindings["report_date"]),
        profile="daily",
        rerun=_rerun(bindings["public_run_id"]),
        delivery_mode=bindings["delivery_mode"],
        insight_lookback_hours=policy.insight_lookback_hours,
        telemetry_resource_set=policy.telemetry_resource_set,
        test_region=registry_binding["test_region"],
        test_region_registry=registry_binding["test_region_registry"],
        catalog_hashes=hashes,
        agent_catalog=agents,
        issue_catalog=issues,
        selected=bindings["selection"],
        registry=registry,
        results=results,
    )
    run_root = _run_root(active, private_root)
    manifest_path = run_root / "run-manifest.json"
    immutable_json(manifest_path, manifest)
    packages = rehydrate_packages(
        manifest,
        issues,
        registry,
        (runtime_factory or LiveRuntime)(profile),
        run_root / "assessment-packages",
        checkpoint_store,
    )
    if len(packages) != 25:
        raise ContractError("Daily composition must produce exactly 25 assessment packages")
    lane_references = {
        receipt["agent_name"]: artifact_reference(
            _lane_receipt_path(
                active,
                private_root,
                receipt["agent_name"],
            ),
            daily_runtime_root(private_root),
            receipt["receipt_digest"],
        )
        for receipt in receipts
    }
    manifest_reference = artifact_reference(
        manifest_path,
        daily_runtime_root(private_root),
        manifest["manifest_hash"],
    )
    active = lifecycle.transition(
        active,
        next_state="COMPOSED",
        artifact_updates={
            "lane_receipts": lane_references,
            "manifest": manifest_reference,
        },
    )
    return {
        **_status(active, private_root),
        "manifest": str(manifest_path),
        "assessment_packages": len(packages),
    }


def validate_daily_assessment_outputs(
    *,
    assessments: Sequence[Path],
    baseline_assessments: Sequence[Path],
    recheck_assessments: Sequence[Path] = (),
    recheck_baseline_assessments: Sequence[Path] = (),
    base: Path | None = None,
) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    active = _read_active(private_root, allowed_states={"COMPOSED"})
    manifest_path, manifest = _active_manifest(active, private_root)
    packages_root = manifest_path.parent / "assessment-packages"
    issue_ids = {
        issue["issue_id"]
        for agent in manifest["agents"]
        for issue in agent["issues"]
    }
    initial_issues = _paths_by_identity(assessments, "issue_id", "issue")
    initial_baselines = _paths_by_identity(
        baseline_assessments,
        "agent_name",
        "baseline",
    )
    loaded_issues = load_assessments(
        list(initial_issues.values()),
        issue_ids,
        packages_root,
        manifest,
    )
    loaded_baselines = load_baseline_assessments(
        list(initial_baselines.values()),
        packages_root,
        manifest,
    )
    recheck_issue_targets = {
        issue_id
        for issue_id, value in loaded_issues.items()
        if value["finding_type"] == "INCOMPLETE"
        and _issue_recheck_eligible(read_json(packages_root / f"{issue_id}.json"))
    }
    recheck_baseline_targets = {
        agent_name
        for agent_name, value in loaded_baselines.items()
        if value["verdict"] == "inconclusive"
        and _baseline_recheck_eligible(
            read_json(packages_root / f"baseline-{agent_name}.json")
        )
    }
    issue_rechecks = _paths_by_identity(
        recheck_assessments,
        "issue_id",
        "issue recheck",
    )
    baseline_rechecks = _paths_by_identity(
        recheck_baseline_assessments,
        "agent_name",
        "baseline recheck",
    )
    if set(issue_rechecks) != recheck_issue_targets:
        raise ContractError(
            "Focused issue rechecks must exactly cover eligible INCOMPLETE assessments"
        )
    if set(baseline_rechecks) != recheck_baseline_targets:
        raise ContractError(
            "Focused baseline rechecks must exactly cover eligible inconclusive assessments"
        )
    if any(
        issue_rechecks[identity].resolve() == initial_issues[identity].resolve()
        for identity in recheck_issue_targets
    ) or any(
        baseline_rechecks[identity].resolve()
        == initial_baselines[identity].resolve()
        for identity in recheck_baseline_targets
    ):
        raise ContractError("Focused recheck output must be a distinct immutable artifact")
    final_issue_paths = {**initial_issues, **issue_rechecks}
    final_baseline_paths = {**initial_baselines, **baseline_rechecks}
    load_assessments(
        list(final_issue_paths.values()),
        issue_ids,
        packages_root,
        manifest,
    )
    load_baseline_assessments(
        list(final_baseline_paths.values()),
        packages_root,
        manifest,
    )
    index = {
        "schema_version": "1.0.0",
        "kind": "daily-assessment-index",
        "manifest_hash": manifest["manifest_hash"],
        "issue_assessments": [
            _file_binding(final_issue_paths[issue_id], private_root, issue_id)
            for issue_id in sorted(final_issue_paths)
        ],
        "baseline_assessments": [
            _file_binding(
                final_baseline_paths[agent_name],
                private_root,
                agent_name,
            )
            for agent_name in AGENT_ORDER
        ],
        "focused_rechecks": [
            *sorted(recheck_issue_targets),
            *[f"baseline:{name}" for name in AGENT_ORDER if name in recheck_baseline_targets],
        ],
        "artifact_digest": "",
    }
    index["artifact_digest"] = content_hash(
        {key: item for key, item in index.items() if key != "artifact_digest"}
    )
    index_path = _run_root(active, private_root) / "assessment-index.json"
    immutable_json(index_path, index)
    reference = artifact_reference(
        index_path,
        daily_runtime_root(private_root),
        index["artifact_digest"],
    )
    lock = _coordinator_lock(private_root)
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=private_root)
        current = lifecycle.read_active()
        if current.digest != active.digest:
            raise ContractError("Daily lifecycle changed during assessment validation")
        active = lifecycle.transition(
            current,
            next_state="ASSESSMENTS_VALIDATED",
            artifact_updates={"assessment_index": reference},
        )
    return {
        **_status(active, private_root),
        "focused_rechecks": index["focused_rechecks"],
    }


def assert_daily_finalization_inputs(
    *,
    manifest_path: Path,
    assessments: Sequence[Path],
    baseline_assessments: Sequence[Path],
    prepare_improvement_input: bool,
    base: Path | None = None,
) -> DailyRecord | None:
    private_root = (base or runtime_root()).resolve()
    manifest = read_json(manifest_path)
    if manifest.get("profile") != "daily":
        return None
    active = _read_optional(private_root)
    if active is None or active.value["artifacts"]["manifest"] is None:
        raise ContractError(
            "Daily finalization requires the active coordinator lifecycle"
        )
    expected_manifest, _ = _active_manifest(active, private_root)
    if manifest_path.resolve() != expected_manifest.resolve():
        raise ContractError("Daily finalization manifest is not the active composition")
    expected_state = (
        "ASSESSMENTS_VALIDATED"
        if prepare_improvement_input
        else "IMPROVEMENT_INPUT_READY"
    )
    if active.value["state"] != expected_state:
        raise ContractError(
            f"Daily finalization stage requires lifecycle {expected_state}"
        )
    index = _active_assessment_index(active, private_root)
    actual_issue = {_file_binding(path, private_root, "")["content_digest"] for path in assessments}
    actual_baseline = {
        _file_binding(path, private_root, "")["content_digest"]
        for path in baseline_assessments
    }
    expected_issue = {
        item["content_digest"] for item in index["issue_assessments"]
    }
    expected_baseline = {
        item["content_digest"] for item in index["baseline_assessments"]
    }
    if actual_issue != expected_issue or actual_baseline != expected_baseline:
        raise ContractError(
            "Daily finalization assessments do not match the validated assessment index"
        )
    return active


def record_daily_improvement_input(
    active: DailyRecord,
    path: Path,
    *,
    base: Path | None = None,
) -> None:
    private_root = (base or runtime_root()).resolve()
    reference = artifact_reference(
        path,
        daily_runtime_root(private_root),
        file_hash(path),
    )
    _transition_exact(
        active,
        private_root,
        "IMPROVEMENT_INPUT_READY",
        {"improvement_input": reference},
    )


def record_daily_finalization(
    active: DailyRecord,
    *,
    report_path: Path,
    email_request_path: Path,
    improvement_analysis_path: Path,
    adx_publication_status: str,
    preview_publication_path: Path | None = None,
    base: Path | None = None,
) -> None:
    private_root = (base or runtime_root()).resolve()
    request = read_json(email_request_path)
    preview_reference = None
    if active.value["bindings"]["publish_preview"]:
        if preview_publication_path is None:
            raise ContractError("Daily preview publication binding is missing")
        publication = read_json(preview_publication_path)
        validate_preview_publication(
            publication,
            run_id=active.value["bindings"]["public_run_id"],
        )
        if request.get("preview") != publication:
            raise ContractError("Daily email request preview binding is stale")
        preview_reference = artifact_reference(
            preview_publication_path,
            daily_runtime_root(private_root),
            file_hash(preview_publication_path),
        )
    elif preview_publication_path is not None or "preview" in request:
        raise ContractError("Daily run did not authorize GitHub preview publication")
    _transition_exact(
        active,
        private_root,
        "FINALIZED",
        {
            "improvement_analysis": artifact_reference(
                improvement_analysis_path,
                daily_runtime_root(private_root),
                file_hash(improvement_analysis_path),
            ),
            "final_report": artifact_reference(
                report_path,
                daily_runtime_root(private_root),
                file_hash(report_path),
            ),
            "adx_publication_status": adx_publication_status,
            "email_request": artifact_reference(
                email_request_path,
                daily_runtime_root(private_root),
                file_hash(email_request_path),
            ),
            "preview_publication": preview_reference,
        },
    )


def claim_daily_email(
    *,
    base: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    lock = _coordinator_lock(private_root)
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=private_root)
        active = lifecycle.read_active()
        if active.value["state"] != "FINALIZED":
            raise ContractError(
                "Daily email send requires the finalized lifecycle exactly once"
            )
        request_path, request = _active_email_request(active, private_root)
        claim_path = _run_root(active, private_root) / "email-send-claim.json"
        if claim_path.is_file():
            claim = read_json(claim_path)
            if (
                claim.get("request_content_digest") != request["content_digest"]
                or claim.get("claim_digest")
                != content_hash(
                    {
                        key: item
                        for key, item in claim.items()
                        if key != "claim_digest"
                    }
                )
            ):
                raise ContractError("Existing Daily email send claim is invalid")
        else:
            claim = {
                "schema_version": "1.0.0",
                "kind": "daily-email-send-claim",
                "request_content_digest": request["content_digest"],
                "claimed_at": now().astimezone(UTC).isoformat(),
                "claim_nonce": uuid.uuid4().hex,
                "claim_digest": "",
            }
            claim["claim_digest"] = content_hash(
                {key: item for key, item in claim.items() if key != "claim_digest"}
            )
            immutable_json(claim_path, claim)
        updated = lifecycle.transition(
            active,
            next_state="SEND_CLAIMED",
            artifact_updates={
                "send_claim": artifact_reference(
                    claim_path,
                    daily_runtime_root(private_root),
                    claim["claim_digest"],
                )
            },
        )
    return {
        **_status(updated, private_root),
        "email_request": str(request_path),
        "send_claim": str(claim_path),
    }


def assert_daily_receipt_import(
    request_path: Path,
    output_path: Path,
    *,
    base: Path | None = None,
) -> DailyRecord | None:
    private_root = (base or runtime_root()).resolve()
    active = _read_optional(private_root)
    if active is None or active.value["artifacts"]["email_request"] is None:
        return None
    expected_request, _ = _active_email_request(active, private_root)
    if request_path.resolve() != expected_request.resolve():
        return None
    if active.value["state"] != "SEND_CLAIMED":
        raise ContractError("Daily email receipt import requires one active send claim")
    expected_output = _run_root(active, private_root) / "email-receipt.json"
    if output_path.resolve() != expected_output.resolve():
        raise ContractError("Daily email receipt must use its canonical private path")
    return active


def record_daily_email_receipt(
    active: DailyRecord,
    receipt_path: Path,
    *,
    base: Path | None = None,
) -> None:
    private_root = (base or runtime_root()).resolve()
    updated = _transition_exact(
        active,
        private_root,
        "RECEIPT_IMPORTED",
        {
            "email_receipt": artifact_reference(
                receipt_path,
                daily_runtime_root(private_root),
                file_hash(receipt_path),
            )
        },
    )
    if updated.value["bindings"]["delivery_mode"] == TEST_EMAIL_ONLY_DELIVERY:
        _transition_exact(updated, private_root, "COMPLETE", {})


def complete_daily_publication(
    *,
    pr_number: int,
    generated_paths: Sequence[str],
    base: Path | None = None,
) -> dict[str, Any]:
    if isinstance(pr_number, bool) or pr_number < 1:
        raise ContractError("Daily publication requires one pull request number")
    private_root = (base or runtime_root()).resolve()
    active = _read_active(private_root, allowed_states={"RECEIPT_IMPORTED"})
    receipt_reference = active.value["artifacts"]["email_receipt"]
    if receipt_reference is None:
        raise ContractError("Daily publication lacks its private delivery receipt")
    receipt_path = (
        daily_runtime_root(private_root) / receipt_reference["path"]
    ).resolve()
    receipt = read_json(receipt_path)
    if receipt["status"] != "sent":
        raise ContractError("Daily publication requires confirmed one-time email delivery")
    validate_generated_paths(list(generated_paths))
    publication = {
        "schema_version": "1.0.0",
        "kind": "daily-publication-receipt",
        "pr_number": pr_number,
        "generated_paths_digest": content_hash(sorted(generated_paths)),
        "publication_digest": "",
    }
    publication["publication_digest"] = content_hash(
        {key: item for key, item in publication.items() if key != "publication_digest"}
    )
    path = _run_root(active, private_root) / "publication-receipt.json"
    immutable_json(path, publication)
    updated = _transition_exact(
        active,
        private_root,
        "COMPLETE",
        {
            "publication": artifact_reference(
                path,
                daily_runtime_root(private_root),
                publication["publication_digest"],
            )
        },
    )
    return _status(updated, private_root)


def fail_daily(
    *,
    reason_code: str,
    confirmed: bool,
    base: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("Daily failure requires explicit --confirm")
    if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", reason_code) is None:
        raise ContractError("Daily failure reason code is not public-safe")
    private_root = (base or runtime_root()).resolve()
    lock = _coordinator_lock(private_root)
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=private_root)
        active = lifecycle.read_active()
        if active.value["state"] in {"COMPLETE", "FAILED"}:
            raise ContractError("Daily lifecycle is already terminal")
        failure = {
            "schema_version": "1.0.0",
            "kind": "daily-failure-receipt",
            "report_date": active.value["bindings"]["report_date"],
            "failed_state": active.value["state"],
            "reason_code": reason_code,
            "failed_at": now().astimezone(UTC).isoformat(),
            "failure_digest": "",
        }
        failure["failure_digest"] = content_hash(
            {key: item for key, item in failure.items() if key != "failure_digest"}
        )
        path = _run_root(active, private_root) / "failure-receipt.json"
        immutable_json(path, failure)
        updated = lifecycle.transition(
            active,
            next_state="FAILED",
            artifact_updates={
                "failure": artifact_reference(
                    path,
                    daily_runtime_root(private_root),
                    failure["failure_digest"],
                )
            },
        )
    return _status(updated, private_root)


def daily_status(*, base: Path | None = None) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    try:
        active = _read_optional(private_root)
    except (ContractError, OSError, ValueError):
        return {
            "state": "FORMAT_REQUIRES_SUPERSESSION",
            "next": "daily-prepare",
        }
    if active is None:
        return {"state": "NOT_PREPARED", "next": "daily-prepare"}
    return _status(active, private_root)


def daily_guide(*, base: Path | None = None) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    active = _read_optional(private_root)
    if active is None:
        return {
            "state": "NOT_PREPARED",
            "next": "Run daily-prepare from the central coordinator session.",
            "internal_fanout": False,
        }
    status = _status(active, private_root)
    state = active.value["state"]
    guide: dict[str, Any] = {
        **status,
        "internal_fanout": False,
        "max_parallel_agents": active.value["bindings"]["policy"][
            "max_parallel_agents"
        ],
    }
    if state not in {"COMPLETE", "FAILED"}:
        guide["fail_command"] = (
            "python -m agent_insights_quality daily-fail "
            "--reason-code <public_safe_code> --confirm"
        )
    if state == "LOCKED":
        guide["next"] = "Run daily-provision in the central coordinator session."
    elif state in {"PREPARED", "TRAFFIC"}:
        guide["agent_lanes"] = [
            {
                "name": _DISPLAY_NAMES[agent_name],
                "agent": agent_name,
                "issues": active.value["bindings"]["selection"][agent_name],
                "command": (
                    "python -m agent_insights_quality daily-run-agent "
                    f"--agent {agent_name}"
                ),
            }
            for agent_name in AGENT_ORDER
            if agent_name in status["pending_agent_lanes"]
        ]
        guide["next"] = (
            "Start one visible Copilot sub session per pending Agent lane; "
            "do not split versions or start subprocess workers."
        )
    elif state == "COMPOSED":
        guide["assessment_lanes"] = [
            {
                "name": _DISPLAY_NAMES[agent_name],
                "agent": agent_name,
                "package_prefixes": [f"baseline-{agent_name}", *active.value["bindings"]["selection"][agent_name]],
            }
            for agent_name in AGENT_ORDER
        ]
        guide["next"] = (
            "Run up to five visible Agent assessment sub sessions, then "
            "daily-validate-assessments with any exact eligible focused rechecks."
        )
    elif state == "ASSESSMENTS_VALIDATED":
        guide["next"] = "Run finalize --prepare-improvement-input centrally."
    elif state == "IMPROVEMENT_INPUT_READY":
        guide["next"] = (
            "Run one visible GPT-5.6 Sol improvement-analysis session, then "
            "finalize centrally with its schema-valid output."
        )
    elif state == "FINALIZED":
        guide["next"] = "Run daily-email-claim once, then send that exact HTML request."
    elif state == "SEND_CLAIMED":
        guide["next"] = (
            "Import exactly one sent, failed, or unknown provider receipt; "
            "never retry an ambiguous send."
        )
    elif state == "RECEIPT_IMPORTED":
        guide["next"] = (
            "Validate generated-only paths, create one Daily pull request, then "
            "register it with daily-complete-publication."
        )
    else:
        guide["next"] = None
    return guide


def _fetch_approved_record() -> dict[str, Any]:
    profile = RuntimeProfile.from_env("daily", TELEMETRY_RESOURCE_SET)
    if (
        profile.name != "daily"
        or profile.environment_id != ENVIRONMENT_ID
        or profile.location != PROFILE_LOCATION
        or profile.telemetry_resource_set != TELEMETRY_RESOURCE_SET
    ):
        raise ContractError(
            "Daily approval records require the reviewed Sweden g30 profile"
        )
    operator = local_azure_operator()
    return fetch_approved_record_for_checkout(
        AzureValidationBlobStore(
            profile.registry_storage_account_name,
            credential=operator.credential,
        ),
        expected_repository=REPOSITORY,
    )


class _FencedRuntime:
    def __init__(self, runtime: Any, fence: Callable[[], None]) -> None:
        self._runtime = runtime
        self._fence = fence

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._runtime, name)
        if not callable(value):
            return value

        def fenced(*args: Any, **kwargs: Any) -> Any:
            self._fence()
            result = value(*args, **kwargs)
            self._fence()
            return result

        return fenced


def _daily_fence(
    expected: DailyRecord,
    base: Path,
    *,
    allowed_states: set[str],
) -> None:
    current = DailyLifecycle(
        lock=_coordinator_lock(base),
        base=base,
    ).read_active()
    expected_bindings = expected.value["bindings"]
    current_bindings = current.value["bindings"]
    if (
        current.value["state"] not in allowed_states
        or current.value["execution_id"] != expected.value["execution_id"]
        or current_bindings["approval"] != expected_bindings["approval"]
        or current_bindings["run_contract_digest"]
        != expected_bindings["run_contract_digest"]
    ):
        raise ContractError("Stale Daily worker is fenced from the active lifecycle")


def _acquire_lane_capacity(active: DailyRecord, base: Path) -> DailyLock:
    maximum = active.value["bindings"]["policy"]["max_parallel_agents"]
    for slot in range(1, maximum + 1):
        lock = DailyLock(
            _run_root(active, base)
            / "lane-capacity"
            / f"slot-{slot:02d}.lock"
        )
        try:
            lock.acquire()
        except ContractError:
            continue
        return lock
    raise ContractError("Daily parallel Agent limit is already active")


def _policy_binding(policy: AutomationPolicy) -> dict[str, Any]:
    return {
        "max_parallel_agents": policy.max_parallel_agents,
        "max_recovery_versions": policy.max_recovery_versions,
        "agent_start_stagger_seconds": policy.agent_start_stagger_seconds,
        "insight_lookback_hours": policy.insight_lookback_hours,
        "clean_window_poll_seconds": policy.clean_window_poll_seconds,
        "clean_window_ingestion_margin_seconds": (
            policy.clean_window_ingestion_margin_seconds
        ),
        "clean_window_max_wait_seconds": policy.clean_window_max_wait_seconds,
        "trace_assertion_stabilization_seconds": (
            policy.trace_assertion_stabilization_seconds
        ),
        "insight_start_margin_seconds": policy.insight_start_margin_seconds,
        "telemetry_resource_set": policy.telemetry_resource_set,
    }


def _daily_contract_digest(
    *,
    bindings: Mapping[str, Any],
    registry: Mapping[str, Any],
    test_region: str,
    superseded_format_digest: str | None = None,
) -> str:
    runtime_files = {
        path.relative_to(ROOT).as_posix(): file_hash(path)
        for path in sorted((ROOT / "src" / "agent_insights_quality").glob("*.py"))
    }
    for relative in (
        "config/automation.yaml",
        "schemas/run-manifest.schema.json",
        "schemas/daily-lifecycle.schema.json",
        "schemas/daily-agent-receipt.schema.json",
        "schemas/daily-email-test-preview.schema.json",
        "schemas/prompt-traffic.schema.json",
        "schemas/assessment-package.schema.json",
        "src/agent_insights_quality/prompts/assessment.md",
    ):
        runtime_files[relative] = file_hash(ROOT / relative)
    return content_hash(
        {
            "schema_version": "4.0.0",
            "repository": bindings["repository"],
            "approval": bindings["approval"],
            "public_run_id": bindings["public_run_id"],
            "report_date": bindings["report_date"],
            "delivery_mode": bindings["delivery_mode"],
            "publish_preview": bindings["publish_preview"],
            "work_items_digest": bindings["work_items"]["content_digest"],
            "catalog_hashes": bindings["catalog_hashes"],
            "selection": bindings["selection"],
            "policy": bindings["policy"],
            "registry_digest": content_hash(registry),
            "test_region": test_region,
            "superseded_format_digest": superseded_format_digest,
            "runtime_files": runtime_files,
        }
    )


def _coordinator_lock(base: Path) -> DailyLock:
    return DailyLock(daily_runtime_root(base) / "coordinator.lock")


def _read_optional(base: Path) -> DailyRecord | None:
    lock = _coordinator_lock(base)
    return DailyLifecycle(lock=lock, base=base).read_optional()


def _read_active(base: Path, *, allowed_states: set[str]) -> DailyRecord:
    active = DailyLifecycle(lock=_coordinator_lock(base), base=base).read_active()
    if active.value["state"] not in allowed_states:
        raise ContractError(
            "Daily lifecycle is not ready for this stage "
            f"(current: {active.value['state']})"
        )
    return active


def _assert_checkout_binding(active: DailyRecord) -> None:
    agents, issues = load_catalogs()
    bindings = active.value["bindings"]
    approval = bindings["approval"]
    commit_sha = current_clean_commit()
    if (
        commit_sha != approval["checkout_commit_sha"]
        or catalog_hashes(agents, issues) != bindings["catalog_hashes"]
        or current_validation_digest(agents, issues)
        != approval["validation_digest"]
    ):
        raise ContractError("Stale Daily worker is fenced from the active lifecycle")


def _run_root(active: DailyRecord, base: Path) -> Path:
    return (
        daily_runtime_root(base)
        / "runs"
        / active.value["bindings"]["public_run_id"]
    )


def _lane_root(active: DailyRecord, base: Path, agent_name: str) -> Path:
    return _run_root(active, base) / "lanes" / agent_name


def _lane_receipt_path(active: DailyRecord, base: Path, agent_name: str) -> Path:
    return _lane_root(active, base, agent_name) / "completion-receipt.json"


def _checkpoint_store(active: DailyRecord, base: Path) -> VersionCheckpointStore:
    digest = active.value["bindings"]["run_contract_digest"]
    if not isinstance(digest, str):
        raise ContractError("Daily run contract is not prepared")
    return VersionCheckpointStore(_run_root(active, base) / "stage-checkpoints", digest)


def _seed(active: DailyRecord) -> int:
    return int(
        active.value["bindings"]["catalog_hashes"]["issues"].split(":", 1)[1][:16],
        16,
    )


def _assert_unique_response_references(
    registry: Mapping[str, Any],
    results: Sequence[AgentResult],
    checkpoint_store: VersionCheckpointStore,
) -> None:
    references: list[str] = []
    for result in results:
        versions = [result.baseline, *result.issues]
        for version in versions:
            entry = version_entry(
                registry,
                result.agent_name,
                version.logical_version,
            )
            invocation = checkpoint_store.invocation(
                result.agent_name,
                version.logical_version,
                entry["foundry_version"],
                entry["content_digest"],
            )
            if invocation is None:
                if version.endpoint_request_count:
                    raise ContractError(
                        "Daily response identity proof is missing from a lane checkpoint"
                    )
                continue
            if len(invocation.response_references) != invocation.request_count:
                raise ContractError("Daily response identity coverage is incomplete")
            references.extend(invocation.response_references)
    if len(references) != len(set(references)):
        raise ContractError("Daily Agent lanes reused a response identity")


def _stamp_lane_receipt(
    active: DailyRecord,
    agent_name: str,
    result: AgentResult,
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-agent-lane-receipt",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": agent_name,
        "issue_ids": active.value["bindings"]["selection"][agent_name],
        "result": asdict(result),
        "receipt_digest": "",
    }
    value["receipt_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "receipt_digest"}
    )
    return value


def _validate_lane_receipt(
    value: Mapping[str, Any],
    active: DailyRecord,
    agent_name: str,
) -> None:
    errors = list(
        Draft202012Validator(read_json(_RECEIPT_SCHEMA)).iter_errors(value)
    )
    if errors:
        raise ContractError(f"Daily Agent lane receipt is invalid: {errors[0].message}")
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"]
        != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != agent_name
        or value["issue_ids"] != active.value["bindings"]["selection"][agent_name]
        or value["receipt_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "receipt_digest"}
        )
    ):
        raise ContractError("Stale Daily Agent lane receipt is fenced")
    result = value["result"]
    if (
        result.get("agent_name") != agent_name
        or result.get("baseline", {}).get("logical_version") != "v0"
        or [item.get("logical_version") for item in result.get("issues", [])]
        != value["issue_ids"]
    ):
        raise ContractError("Daily Agent lane result coverage is inconsistent")
    operations = [
        operation
        for item in [result["baseline"], *result["issues"]]
        for operation in item.get("operation_ids", [])
    ]
    if len(operations) != len(set(operations)):
        raise ContractError("Daily Agent lane reused an operation identity")


def _read_lane_receipt(
    path: Path,
    active: DailyRecord,
    agent_name: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"{agent_name} Daily Agent lane receipt is missing")
    value = read_json(path)
    _validate_lane_receipt(value, active, agent_name)
    return value


def _lane_result(receipt: Mapping[str, Any], *, resumed: bool) -> dict[str, Any]:
    result = receipt["result"]
    versions = [result["baseline"], *result["issues"]]
    return {
        "agent": receipt["agent_name"],
        "status": "already_complete" if resumed else "complete",
        "versions": len(versions),
        "incomplete_versions": sum(
            item["status"] in {"inconclusive", "skipped_baseline"}
            for item in versions
        ),
        "receipt_digest": receipt["receipt_digest"],
    }


def _agent_result(value: Mapping[str, Any]) -> AgentResult:
    return AgentResult(
        agent_name=str(value["agent_name"]),
        baseline=_version_result(value["baseline"]),
        issues=[_version_result(item) for item in value["issues"]],
    )


def _version_result(value: Mapping[str, Any]) -> VersionResult:
    return VersionResult(
        logical_version=str(value["logical_version"]),
        foundry_version=str(value["foundry_version"]),
        status=str(value["status"]),
        operation_ids=list(value.get("operation_ids") or []),
        insight_references=list(value.get("insight_references") or []),
        window_start=value.get("window_start"),
        window_end=value.get("window_end"),
        error_code=value.get("error_code"),
        observed_insight=(
            _insight(value["observed_insight"])
            if isinstance(value.get("observed_insight"), Mapping)
            else None
        ),
        observed_insights=[
            _insight(item) for item in value.get("observed_insights", [])
        ],
        endpoint_request_count=int(value.get("endpoint_request_count") or 0),
        endpoint_response_count=int(value.get("endpoint_response_count") or 0),
        endpoint_usable_response_count=int(
            value.get("endpoint_usable_response_count") or 0
        ),
        semantic_assertion_count=int(value.get("semantic_assertion_count") or 0),
        semantic_assertions_passed=int(
            value.get("semantic_assertions_passed") or 0
        ),
        trace_assertion_count=int(value.get("trace_assertion_count") or 0),
        trace_assertions_passed=int(value.get("trace_assertions_passed") or 0),
        trace_contract_verified=bool(value.get("trace_contract_verified")),
        trace_behavior_summary=dict(value.get("trace_behavior_summary") or {}),
        endpoint_request_summaries=[
            RequestCompletionEvidence(
                request_index=int(item["request_index"]),
                response_count=int(item["response_count"]),
                usable_response=bool(item["usable_response"]),
                semantic_assertion_count=int(item["semantic_assertion_count"]),
                semantic_assertions_passed=int(item["semantic_assertions_passed"]),
                assertion_results=tuple(
                    SemanticAssertionEvidence(
                        assertion=str(result["assertion"]),
                        passed=bool(result["passed"]),
                    )
                    for result in item["assertion_results"]
                ),
                activation_gate=bool(item["activation_gate"]),
                direct_terminal_response_count=int(
                    item["direct_terminal_response_count"]
                ),
                function_call_count=int(item["function_call_count"]),
                trace_assertion_count=int(item["trace_assertion_count"]),
                trace_assertions_passed=int(item["trace_assertions_passed"]),
                trace_assertion_results=tuple(
                    TraceAssertionEvidence(
                        assertion=str(result["assertion"]),
                        passed=bool(result["passed"]),
                    )
                    for result in item["trace_assertion_results"]
                ),
            )
            for item in value.get("endpoint_request_summaries", [])
        ],
    )


def _insight(value: Mapping[str, Any]) -> InsightEvidence:
    return InsightEvidence(
        reference=str(value["reference"]),
        agent_version=str(value["agent_version"]),
        title=str(value["title"]),
        description=str(value["description"]),
        category=str(value["category"]),
        severity=str(value["severity"]),
        proposed_fix=str(value["proposed_fix"]),
        linked_operation_ids=tuple(value["linked_operation_ids"]),
        trace_count=int(value["trace_count"]),
        updated_at=str(value["updated_at"]),
    )


def _active_manifest(
    active: DailyRecord,
    base: Path,
) -> tuple[Path, dict[str, Any]]:
    reference = active.value["artifacts"]["manifest"]
    if reference is None:
        raise ContractError("Daily lifecycle has no composed manifest")
    path = (daily_runtime_root(base) / reference["path"]).resolve()
    manifest = read_json(path)
    validate_manifest(manifest)
    if manifest["manifest_hash"] != reference["digest"]:
        raise ContractError("Daily manifest reference is stale")
    return path, manifest


def _active_assessment_index(active: DailyRecord, base: Path) -> dict[str, Any]:
    reference = active.value["artifacts"]["assessment_index"]
    if reference is None:
        raise ContractError("Daily lifecycle has no assessment index")
    path = (daily_runtime_root(base) / reference["path"]).resolve()
    value = read_json(path)
    if (
        value.get("artifact_digest") != reference["digest"]
        or value.get("artifact_digest")
        != content_hash(
            {key: item for key, item in value.items() if key != "artifact_digest"}
        )
    ):
        raise ContractError("Daily assessment index reference is stale")
    return value


def _active_email_request(
    active: DailyRecord,
    base: Path,
) -> tuple[Path, dict[str, Any]]:
    reference = active.value["artifacts"]["email_request"]
    if reference is None:
        raise ContractError("Daily lifecycle has no email request")
    path = (daily_runtime_root(base) / reference["path"]).resolve()
    request = read_json(path)
    if file_hash(path) != reference["digest"]:
        raise ContractError("Daily email request reference is stale")
    return path, request


def _paths_by_identity(
    paths: Sequence[Path],
    field: str,
    label: str,
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in paths:
        identity = str(read_json(path).get(field) or "")
        if not identity or identity in result:
            raise ContractError(f"Duplicate or missing {label} identity")
        result[identity] = path
    return result


def _issue_recheck_eligible(package: Mapping[str, Any]) -> bool:
    endpoint = package.get("endpoint_evidence")
    proof = package.get("full_request_trace_proof")
    if not isinstance(endpoint, Mapping) or not isinstance(proof, Mapping):
        return False
    summaries = endpoint.get("request_summaries")
    activation = [
        item
        for item in summaries or []
        if isinstance(item, Mapping) and item.get("activation_gate") is True
    ]
    issue_id = str(package.get("issue_id") or "")
    activation_required = (
        issue_id in {f"issue-{number:03d}" for number in range(1, 13)}
        or bool(activation)
    )
    return (
        package.get("runtime_status") in {"observed", "not_at_bar"}
        and _endpoint_evidence_complete(dict(endpoint))
        and int(proof.get("operation_count") or 0)
        >= int(package.get("expected", {}).get("minimum_traces") or 0)
        and (
            not activation_required
            or _issue_activation_complete(dict(package))
        )
    )


def _baseline_recheck_eligible(package: Mapping[str, Any]) -> bool:
    return (
        package.get("runtime_status") in {"passed", "not_at_bar"}
        and _baseline_evidence_complete(dict(package))
    )


def _file_binding(path: Path, base: Path, identity: str) -> dict[str, str]:
    resolved = path.resolve()
    if base != resolved and base not in resolved.parents:
        raise ContractError("Daily assessment output must stay in the private runtime root")
    return {
        "identity": identity,
        "path": resolved.relative_to(base).as_posix(),
        "content_digest": file_hash(resolved),
    }


def _transition_exact(
    active: DailyRecord,
    base: Path,
    next_state: str,
    artifacts: Mapping[str, Any],
) -> DailyRecord:
    lock = _coordinator_lock(base)
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=base)
        current = lifecycle.read_active()
        if current.digest != active.digest:
            raise ContractError("Daily lifecycle changed before stage completion")
        return lifecycle.transition(
            current,
            next_state=next_state,
            artifact_updates=artifacts,
        )


def _status(active: DailyRecord, base: Path) -> dict[str, Any]:
    completed = [
        agent_name
        for agent_name in AGENT_ORDER
        if _lane_receipt_path(active, base, agent_name).is_file()
    ]
    pending = [agent_name for agent_name in AGENT_ORDER if agent_name not in completed]
    return {
        "state": active.value["state"],
        "report_date": active.value["bindings"]["report_date"],
        "delivery_mode": active.value["bindings"]["delivery_mode"],
        "publish_preview": active.value["bindings"]["publish_preview"],
        "completed_agent_lanes": completed,
        "pending_agent_lanes": pending,
        "package_target": 25,
        "adx_publication_status": active.value["artifacts"][
            "adx_publication_status"
        ],
    }


def _rerun(public_run_id: str) -> int:
    marker = public_run_id.rpartition("-r")
    return int(marker[2]) if marker[1] else 0
