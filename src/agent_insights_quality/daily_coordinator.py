from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from agent_insights_quality.baseline_policy import baseline_terminal_decision
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
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    VersionResult,
    linked_operations_match_scope,
    request_completion_payload,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.provisioning import build_artifact, provision_profile
from agent_insights_quality.registry import (
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
from agent_insights_quality.runner import (
    _baseline_evidence_is_strict,
    execute_agent,
    issue_usable_response_unknown_accepted,
    resume_issue_version,
)
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.selection import select_daily
from agent_insights_quality.validation_rules import issue_observation_context
from agent_insights_quality.validation_trace_gap_policy import (
    daily_issue_side_decision,
)
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    file_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_local import current_clean_commit
from agent_insights_quality.work_items import load_quality_work_items

REPOSITORY = "ninghu/agent-insights-quality"
_RECEIPT_SCHEMA = ROOT / "schemas" / "daily-agent-receipt.schema.json"
_RECOVERY_SCHEMA = ROOT / "schemas" / "daily-lane-recovery.schema.json"
_REOPEN_SCHEMA = ROOT / "schemas" / "daily-lane-reopen.schema.json"
_RECOVERY_RECEIPT_SCHEMA = (
    ROOT / "schemas" / "daily-agent-recovery-receipt.schema.json"
)
_WORKER_CLAIM_SCHEMA = ROOT / "schemas" / "daily-lane-worker-claim.schema.json"
_VERSION_REOPEN_SCHEMA = ROOT / "schemas" / "daily-version-reopen.schema.json"
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
    checkout_commit_sha = current_clean_commit()
    resolved_work_items = work_items_path.resolve()
    if private_root != resolved_work_items and private_root not in resolved_work_items.parents:
        raise ContractError("Daily work-item snapshot must stay in the private runtime root")
    moment = moment_value.astimezone(UTC).isoformat()
    delivery_mode = TEST_EMAIL_ONLY_DELIVERY if test_run else OFFICIAL_DELIVERY
    initial = {
        "schema_version": "5.0.0",
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
            "checkout_commit_sha": checkout_commit_sha,
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
    _assert_checkout_binding(active, private_root)
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
    reopen = _read_lane_reopen(active, private_root, agent_name)
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
        receipt_path = (
            _lane_recovery_receipt_path(active, private_root, agent_name)
            if reopen is not None
            else _lane_receipt_path(active, private_root, agent_name)
        )
        if receipt_path.is_file():
            receipt = _read_lane_receipt(
                receipt_path,
                active,
                agent_name,
                reopen=reopen,
            )
            return _lane_result(receipt, resumed=True)
        worker_claim = (
            _claim_reopened_lane_worker(
                active,
                private_root,
                agent_name,
                reopen,
            )
            if reopen is not None
            else None
        )
        _assert_checkout_binding(
            active,
            private_root,
            agent_name=agent_name,
            worker_claim=worker_claim,
        )
        capacity = _acquire_lane_capacity(active, private_root)
        try:
            return _run_daily_agent_locked(
                active=active,
                private_root=private_root,
                agent_name=agent_name,
                receipt_path=receipt_path,
                profile_factory=profile_factory,
                runtime_factory=runtime_factory,
                reopen=reopen,
                worker_claim=worker_claim,
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
    reopen: Mapping[str, Any] | None,
    worker_claim: Mapping[str, Any] | None,
) -> dict[str, Any]:
    _daily_fence(
        active,
        private_root,
        allowed_states={"TRAFFIC"},
        worker_claim=worker_claim,
    )
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
    accepted_baseline = None
    if reopen is not None:
        original = _read_lane_receipt(
            _lane_receipt_path(active, private_root, agent_name),
            active,
            agent_name,
        )
        accepted_baseline = replace(
            _agent_result(original["result"]).baseline,
            status="passed",
            error_code=None,
        )
        baseline_registry = version_entry(registry, agent_name, "v0")
        checkpoint_baseline = checkpoint_store.result(
            agent_name,
            "v0",
            baseline_registry["foundry_version"],
            baseline_registry["content_digest"],
        )
        if (
            checkpoint_baseline is None
            or asdict(checkpoint_baseline) != asdict(accepted_baseline)
            or content_hash(asdict(accepted_baseline))
            != reopen["accepted_baseline_digest"]
        ):
            raise ContractError("Reopened Daily baseline checkpoint is stale")
    runtime = _FencedRuntime(
        (runtime_factory or LiveRuntime)(profile),
        lambda: _daily_fence(
            active,
            private_root,
            allowed_states={"TRAFFIC"},
            worker_claim=worker_claim,
        ),
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
        accepted_baseline=accepted_baseline,
    )
    recovery = _lane_recovery_state(
        active,
        agent_name,
        result,
        checkpoint_store,
        policy.max_recovery_versions,
    )
    if recovery is not None:
        atomic_json(_lane_recovery_path(active, private_root, agent_name), recovery)
        return {
            "agent": agent_name,
            "status": recovery["status"],
            "reason_code": recovery["reason_code"],
            "remaining_recoveries": recovery["remaining_recoveries"],
            "versions": 1 + len(result.issues),
            "incomplete_versions": sum(
                value.status in {"inconclusive", "skipped_baseline"}
                for value in [result.baseline, *result.issues]
            ),
        }
    if checkpoint_store.has_unresolved_insight_state():
        raise ContractError(
            f"{agent_name} has unresolved Agent Insights state; resume its lane"
        )
    _daily_fence(
        active,
        private_root,
        allowed_states={"TRAFFIC"},
        worker_claim=worker_claim,
    )
    receipt = (
        _stamp_lane_recovery_receipt(active, agent_name, result, reopen)
        if reopen is not None
        else _stamp_lane_receipt(active, agent_name, result)
    )
    _validate_lane_receipt(
        receipt,
        active,
        agent_name,
        reopen=reopen,
    )
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
    linkage_batch = _run_root(
        active,
        private_root,
    ) / "card-linkage-reclassification.json"
    if not linkage_batch.is_file():
        raise ContractError(
            "Daily composition requires complete 20-issue card linkage reclassification"
        )
    linkage_value = read_json(linkage_batch)
    if (
        linkage_value.get("execution_id") != active.value["execution_id"]
        or len(linkage_value.get("issue_ids") or []) != 20
        or linkage_value.get("batch_digest")
        != content_hash(
            {
                key: item
                for key, item in linkage_value.items()
                if key != "batch_digest"
            }
        )
    ):
        raise ContractError("Daily card linkage batch receipt is stale")
    _assert_checkout_binding(
        active,
        private_root,
        allow_recovery_commit=True,
    )
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    profile = (profile_factory or RuntimeProfile.from_env)("daily")
    registry = load_registry(
        profile.registry_path,
        profile="daily",
        catalog_hashes=hashes,
    )
    receipt_records = [
        _effective_lane_receipt(active, private_root, agent_name)
        for agent_name in AGENT_ORDER
    ]
    receipts = [item[1] for item in receipt_records]
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
            path,
            daily_runtime_root(private_root),
            receipt["receipt_digest"],
        )
        for path, receipt in receipt_records
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


def reopen_incomplete_daily_lane(
    agent_name: str,
    *,
    confirmed: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("Daily lane reopen requires explicit --confirm")
    if agent_name not in AGENT_ORDER:
        raise ContractError("Daily Agent lane is unknown")
    private_root = (base or runtime_root()).resolve()
    lock = _coordinator_lock(private_root)
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=private_root)
        active = lifecycle.read_active()
        if active.value["state"] not in {"PREPARED", "TRAFFIC"}:
            raise ContractError("Daily lane reopen is no longer allowed")
        current_commit = current_clean_commit()
        agents, issues = load_catalogs()
        hashes = catalog_hashes(agents, issues)
        if hashes != active.value["bindings"]["catalog_hashes"]:
            raise ContractError("Daily lane reopen Agent or traffic content changed")
        profile = RuntimeProfile.from_env("daily")
        registry = load_registry(
            profile.registry_path,
            profile="daily",
            catalog_hashes=hashes,
        )
        registry_binding = active.value["bindings"]["registry"]
        if (
            registry_binding is None
            or content_hash(registry) != registry_binding["content_digest"]
        ):
            raise ContractError("Daily lane reopen deployment binding changed")
        original_path = _lane_receipt_path(active, private_root, agent_name)
        original = _read_lane_receipt(
            original_path,
            active,
            agent_name,
        )
        if _read_active_reopen(active, private_root, agent_name) is not None:
            raise ContractError("Daily Agent lane is already reopened")
        result = _agent_result(original["result"])
        if (
            result.baseline.status != "inconclusive"
            or result.baseline.error_code
            not in {"baseline_evidence_failed", "baseline_evidence_incomplete"}
            or result.baseline.insight_references
            or result.baseline.observed_insights
            or any(
                item.status != "skipped_baseline"
                or item.operation_ids
                or item.endpoint_request_count != 0
                or item.endpoint_response_count != 0
                or item.endpoint_usable_response_count != 0
                or item.insight_references
                or item.observed_insights
                for item in result.issues
            )
        ):
            raise ContractError(
                "Daily lane receipt is not an untouched incomplete-baseline lane"
            )
        checkpoint_store = _checkpoint_store(active, private_root)
        baseline_registry = version_entry(registry, agent_name, "v0")
        checkpoint_args = (
            agent_name,
            "v0",
            baseline_registry["foundry_version"],
            baseline_registry["content_digest"],
        )
        invocation = checkpoint_store.invocation(*checkpoint_args)
        checkpoint_baseline = checkpoint_store.result(*checkpoint_args)
        if (
            checkpoint_baseline is None
            or asdict(checkpoint_baseline) != asdict(result.baseline)
            or not _baseline_evidence_is_strict(result.baseline, invocation)
        ):
            raise ContractError("Daily lane reopen baseline evidence is not exact")
        decision = baseline_terminal_decision(
            request_count=result.baseline.endpoint_request_count,
            terminal_mode=next(
                item["baseline_contract"]["terminal_response"]
                for item in agents["agents"]
                if item["name"] == agent_name
            ),
            trace_evidence=result.baseline.trace_behavior_summary,
            strict_evidence=True,
        )
        if decision.status != "accepted_unknown" or decision.unknown_count != 1:
            raise ContractError(
                "Daily lane baseline is not eligible for single-unknown acceptance"
            )
        accepted_baseline = replace(
            result.baseline,
            status="passed",
            error_code=None,
        )
        event = {
            "schema_version": "1.0.0",
            "kind": "daily-lane-reopen",
            "execution_id": active.value["execution_id"],
            "run_contract_digest": active.value["bindings"]["run_contract_digest"],
            "agent_name": agent_name,
            "assigned_issue_ids": active.value["bindings"]["selection"][agent_name],
            "original_checkout_commit_sha": active.value["bindings"][
                "checkout_commit_sha"
            ],
            "recovery_commit_sha": current_commit,
            "catalog_hashes": hashes,
            "registry_digest": content_hash(registry),
            "original_receipt_digest": original["receipt_digest"],
            "accepted_baseline_digest": content_hash(asdict(accepted_baseline)),
            "recovery_verifier_digest": _lane_recovery_verifier_digest(),
            "event_digest": "",
        }
        event["event_digest"] = content_hash(
            {key: item for key, item in event.items() if key != "event_digest"}
        )
        event_path = _lane_reopen_event_path(
            active,
            private_root,
            agent_name,
            event["event_digest"],
        )
        _validate_lane_reopen(event, active, agent_name)
        immutable_json(event_path, event)
        checkpoint_store.preserve_version_attempt(*checkpoint_args)
        checkpoint_store.save_result(*checkpoint_args, accepted_baseline)
        immutable_json(
            _lane_reopen_pointer_path(active, private_root, agent_name),
            {
                "schema_version": "1.0.0",
                "event_path": event_path.relative_to(
                    daily_runtime_root(private_root)
                ).as_posix(),
                "event_digest": event["event_digest"],
            },
        )
    return {
        **_status(active, private_root),
        "agent": agent_name,
        "status": "reopened",
        "next_command": (
            "python -m agent_insights_quality daily-run-agent "
            f"--agent {agent_name}"
        ),
    }


def reopen_incomplete_daily_version(
    agent_name: str,
    issue_id: str,
    *,
    confirmed: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("Daily version reopen requires explicit --confirm")
    if agent_name not in AGENT_ORDER or re.fullmatch(r"issue-[0-9]{3}", issue_id) is None:
        raise ContractError("Daily version reopen assignment is invalid")
    private_root = (base or runtime_root()).resolve()
    active = _read_active(private_root, allowed_states={"TRAFFIC"})
    lane_lock = DailyLock(_lane_root(active, private_root, agent_name) / "lane.lock")
    with lane_lock:
        agents, issues = load_catalogs()
        hashes = catalog_hashes(agents, issues)
        agent = next(item for item in agents["agents"] if item["name"] == agent_name)
        issue = next(
            (item for item in issues["issues"] if item["id"] == issue_id),
            None,
        )
        if issue is None or issue_id not in active.value["bindings"]["selection"][agent_name]:
            raise ContractError("Daily version reopen issue is not assigned")
        if hashes != active.value["bindings"]["catalog_hashes"]:
            raise ContractError("Daily version reopen Agent or traffic content changed")
        profile = RuntimeProfile.from_env("daily")
        registry = load_registry(
            profile.registry_path,
            profile="daily",
            catalog_hashes=hashes,
        )
        registry_binding = active.value["bindings"]["registry"]
        if (
            registry_binding is None
            or content_hash(registry) != registry_binding["content_digest"]
        ):
            raise ContractError("Daily version reopen deployment binding changed")
        original = _read_lane_receipt(
            _lane_receipt_path(active, private_root, agent_name),
            active,
            agent_name,
        )
        result = _agent_result(original["result"])
        target = next(item for item in result.issues if item.logical_version == issue_id)
        if (
            target.status != "inconclusive"
            or not isinstance(target.error_code, str)
            or target.insight_references
            or target.observed_insights
        ):
            raise ContractError("Daily issue checkpoint is not reopenable")
        if _read_active_reopen(active, private_root, agent_name) is not None:
            raise ContractError("Daily Agent already has an active reopen event")
        store = _checkpoint_store(active, private_root)
        entry = version_entry(registry, agent_name, issue_id)
        checkpoint_args = (
            agent_name,
            issue_id,
            entry["foundry_version"],
            entry["content_digest"],
        )
        invocation = store.invocation(*checkpoint_args)
        if (
            invocation is None
            or store.insight_start_pending(*checkpoint_args)
            or store.insight_drain_pending(*checkpoint_args)
            or not _issue_version_reopenable(
                agent,
                issue,
                target,
                invocation,
            )
        ):
            raise ContractError("Daily issue existing traffic is not safely reclassifiable")
        event = {
            "schema_version": "1.0.0",
            "kind": "daily-version-reopen",
            "execution_id": active.value["execution_id"],
            "run_contract_digest": active.value["bindings"]["run_contract_digest"],
            "agent_name": agent_name,
            "issue_id": issue_id,
            "original_checkout_commit_sha": active.value["bindings"][
                "checkout_commit_sha"
            ],
            "recovery_commit_sha": current_clean_commit(),
            "catalog_hashes": hashes,
            "registry_digest": content_hash(registry),
            "original_receipt_digest": original["receipt_digest"],
            "accepted_baseline_digest": content_hash(original["result"]["baseline"]),
            "invocation_digest": content_hash(asdict(invocation)),
            "recovery_verifier_digest": _lane_recovery_verifier_digest(),
            "event_digest": "",
        }
        event["event_digest"] = content_hash(
            {key: item for key, item in event.items() if key != "event_digest"}
        )
        _validate_version_reopen(event, active, agent_name, issue_id)
        event_path = _version_reopen_event_path(
            active,
            private_root,
            agent_name,
            event["event_digest"],
        )
        immutable_json(event_path, event)
        immutable_json(
            _version_reopen_pointer_path(active, private_root, agent_name),
            {
                "schema_version": "1.0.0",
                "event_path": event_path.relative_to(
                    daily_runtime_root(private_root)
                ).as_posix(),
                "event_digest": event["event_digest"],
            },
        )
    return {
        **_status(active, private_root),
        "agent": agent_name,
        "issue": issue_id,
        "status": "reopened",
        "endpoint_requests": 0,
        "next_command": (
            "python -m agent_insights_quality daily-run-reopened-version "
            f"--agent {agent_name} --issue {issue_id}"
        ),
    }


def _issue_version_reopenable(
    agent: Mapping[str, Any],
    issue: Mapping[str, Any],
    result: VersionResult,
    invocation: InvocationEvidence,
) -> bool:
    error_code = str(result.error_code or "")
    traffic_path = ROOT / str(issue["implementation"]) / "traffic.json"
    if error_code == "endpoint_contract_failed":
        return issue_usable_response_unknown_accepted(
            agent=dict(agent),
            traffic_path=traffic_path,
            invocation=invocation,
        )
    if not error_code.startswith(
        (
            "telemetry_failed",
            "trace_contract_failed",
            "trace_evidence_failed",
            "trace_assertion_failed",
        )
    ):
        return False
    summaries = invocation.request_summaries
    return (
        invocation.request_count > 0
        and invocation.request_count
        == invocation.response_count
        == invocation.usable_response_count
        == len(invocation.response_references)
        == len(summaries)
        and len(set(invocation.response_references))
        == len(invocation.response_references)
        and invocation.allow_window_correlation is False
        and all(
            summary.response_count == 1
            and summary.usable_response
            and summary.semantic_assertions_passed
            == summary.semantic_assertion_count
            and all(
                assertion.passed and assertion.evidence_sufficient
                for assertion in summary.assertion_results
            )
            and all(
                assertion.passed or not assertion.evidence_sufficient
                for assertion in summary.trace_assertion_results
            )
            and (
                agent["type"] != "prompt"
                or summary.function_call_count == 0
            )
            for summary in summaries
        )
        and int(result.trace_behavior_summary.get("unhandled_error_count") or 0)
        == 0
    )


class _NoEndpointInvokeRuntime:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def __getattr__(self, name: str) -> Any:
        if name == "invoke_version":
            raise ContractError("Reopened Daily issue cannot send endpoint traffic")
        return getattr(self._runtime, name)


def run_reopened_daily_version(
    agent_name: str,
    issue_id: str,
    *,
    base: Path | None = None,
    profile_factory: Callable[[str], RuntimeProfile] | None = None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None = None,
) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    active = _read_active(private_root, allowed_states={"TRAFFIC"})
    reopen = _read_version_reopen(active, private_root, agent_name)
    if reopen is None or reopen["issue_id"] != issue_id:
        raise ContractError("Daily issue recovery is not active")
    lane_lock = DailyLock(_lane_root(active, private_root, agent_name) / "lane.lock")
    with lane_lock:
        receipt_path = _lane_recovery_receipt_path(active, private_root, agent_name)
        if receipt_path.is_file():
            receipt = _read_lane_receipt(
                receipt_path,
                active,
                agent_name,
                reopen=reopen,
            )
            return _lane_result(receipt, resumed=True)
        claim = _claim_reopened_lane_worker(
            active,
            private_root,
            agent_name,
            reopen,
        )
        _assert_checkout_binding(
            active,
            private_root,
            agent_name=agent_name,
            worker_claim=claim,
        )
        _daily_fence(
            active,
            private_root,
            allowed_states={"TRAFFIC"},
            worker_claim=claim,
        )
        agents, issues = load_catalogs()
        hashes = catalog_hashes(agents, issues)
        profile = (profile_factory or RuntimeProfile.from_env)("daily")
        registry = load_registry(
            profile.registry_path,
            profile="daily",
            catalog_hashes=hashes,
        )
        if content_hash(registry) != reopen["registry_digest"]:
            raise ContractError("Daily issue recovery registry changed")
        store = _checkpoint_store(active, private_root)
        original = _read_lane_receipt(
            _lane_receipt_path(active, private_root, agent_name),
            active,
            agent_name,
        )
        original_result = _agent_result(original["result"])
        runtime = _FencedRuntime(
            _NoEndpointInvokeRuntime((runtime_factory or LiveRuntime)(profile)),
            lambda: _daily_fence(
                active,
                private_root,
                allowed_states={"TRAFFIC"},
                worker_claim=claim,
            ),
        )
        policy = load_automation_policy()
        recovered = resume_issue_version(
            agent_name=agent_name,
            issue_id=issue_id,
            agents=agents,
            issues=issues,
            registry=registry,
            runtime=runtime,
            seed=_seed(active)
            + active.value["bindings"]["selection"][agent_name].index(issue_id)
            + 1,
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
            checkpoint_store=store,
        )
        merged = AgentResult(
            agent_name,
            original_result.baseline,
            [
                recovered if item.logical_version == issue_id else item
                for item in original_result.issues
            ],
        )
        receipt = _stamp_lane_recovery_receipt(
            active,
            agent_name,
            merged,
            reopen,
        )
        _validate_lane_receipt(
            receipt,
            active,
            agent_name,
            reopen=reopen,
        )
        immutable_json(receipt_path, receipt)
        return _lane_result(receipt, resumed=False)


def _reclassify_completed_daily_version(
    agent_name: str,
    issue_id: str,
    *,
    confirmed: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("Daily version reclassification requires explicit --confirm")
    private_root = (base or runtime_root()).resolve()
    active = _read_active(private_root, allowed_states={"TRAFFIC"})
    if agent_name not in AGENT_ORDER or issue_id not in active.value["bindings"][
        "selection"
    ][agent_name]:
        raise ContractError("Daily version reclassification assignment is invalid")
    lane_lock = DailyLock(_lane_root(active, private_root, agent_name) / "lane.lock")
    with lane_lock:
        if _read_active_reopen(active, private_root, agent_name) is not None:
            raise ContractError("Daily Agent already has an active reopen event")
        agents, issues = load_catalogs()
        current_hashes = catalog_hashes(agents, issues)
        active_hashes = active.value["bindings"]["catalog_hashes"]
        if (
            current_hashes["agents"] != active_hashes["agents"]
            or current_hashes["issues"] != active_hashes["issues"]
        ):
            raise ContractError("Daily reclassification catalog contract changed")
        agent = next(item for item in agents["agents"] if item["name"] == agent_name)
        issue = next(item for item in issues["issues"] if item["id"] == issue_id)
        profile = RuntimeProfile.from_env("daily")
        registry = load_registry(
            profile.registry_path,
            profile="daily",
            catalog_hashes=active_hashes,
        )
        registry_binding = active.value["bindings"]["registry"]
        entry = version_entry(registry, agent_name, issue_id)
        if (
            registry_binding is None
            or content_hash(registry) != registry_binding["content_digest"]
            or build_artifact(agent, issue)["content_digest"]
            != entry["content_digest"]
        ):
            raise ContractError("Daily reclassification Agent or deployment changed")
        original = _read_lane_receipt(
            _lane_receipt_path(active, private_root, agent_name),
            active,
            agent_name,
        )
        original_result = _agent_result(original["result"])
        target = next(
            item for item in original_result.issues if item.logical_version == issue_id
        )
        store = _checkpoint_store(active, private_root)
        checkpoint_args = (
            agent_name,
            issue_id,
            entry["foundry_version"],
            entry["content_digest"],
        )
        checkpoint_result = store.result(*checkpoint_args)
        invocation = store.invocation(*checkpoint_args)
        insight = (
            target.observed_insight
            if target.observed_insight is not None
            else target.observed_insights[0]
            if len(target.observed_insights) == 1
            else None
        )
        linked = list(insight.linked_operation_ids) if insight is not None else []
        context = issue_observation_context(
            ROOT / issue["implementation"] / "traffic.json"
        )
        decided, acceptance = daily_issue_side_decision(
            validation_mode=str(context["validation_mode"]),
            n=int(context["n"]),
            k=int(context["k"]),
            required_surfaces=context["required_surfaces"],
            summaries=[
                request_completion_payload(item)
                for item in target.endpoint_request_summaries
                if item.activation_gate
            ],
            identity_verified=target.trace_contract_verified,
        )
        if (
            target.status != "not_at_bar"
            or target.error_code != "insufficient_trace_evidence"
            or checkpoint_result is None
            or asdict(checkpoint_result) != asdict(target)
            or invocation is None
            or invocation.request_count != invocation.response_count
            or invocation.request_count != invocation.usable_response_count
            or len(invocation.response_references) != invocation.request_count
            or len(set(invocation.response_references))
            != len(invocation.response_references)
            or not decided
            or acceptance is not None
            or insight is None
            or insight.agent_version != entry["foundry_version"]
            or not linked_operations_match_scope(linked, target.operation_ids)
            or insight.trace_count != len(linked)
            or int(target.trace_behavior_summary.get("operation_count") or 0)
            < int(issue["trace_contract"]["minimum_traces"])
            or target.trace_behavior_summary.get("unhandled_error_count", 0) != 0
        ):
            raise ContractError("Daily completed issue is not safely reclassifiable")
        replacement = replace(
            target,
            status="observed",
            error_code=None,
            observed_insight=insight,
        )
        event = {
            "schema_version": "1.0.0",
            "kind": "daily-version-reopen",
            "execution_id": active.value["execution_id"],
            "run_contract_digest": active.value["bindings"]["run_contract_digest"],
            "agent_name": agent_name,
            "issue_id": issue_id,
            "original_checkout_commit_sha": active.value["bindings"][
                "checkout_commit_sha"
            ],
            "recovery_commit_sha": current_clean_commit(),
            "catalog_hashes": active_hashes,
            "registry_digest": content_hash(registry),
            "original_receipt_digest": original["receipt_digest"],
            "accepted_baseline_digest": content_hash(original["result"]["baseline"]),
            "invocation_digest": content_hash(asdict(invocation)),
            "recovery_verifier_digest": _lane_recovery_verifier_digest(),
            "event_digest": "",
        }
        event["event_digest"] = content_hash(
            {key: item for key, item in event.items() if key != "event_digest"}
        )
        _validate_version_reopen(event, active, agent_name, issue_id)
        event_path = _version_reopen_event_path(
            active,
            private_root,
            agent_name,
            event["event_digest"],
        )
        immutable_json(event_path, event)
        store.save_supplemental_result(
            *checkpoint_args,
            replacement,
            event_digest=event["event_digest"],
        )
        merged = AgentResult(
            agent_name,
            original_result.baseline,
            [
                replacement if item.logical_version == issue_id else item
                for item in original_result.issues
            ],
        )
        receipt = _stamp_lane_recovery_receipt(
            active,
            agent_name,
            merged,
            event,
        )
        _validate_lane_receipt(
            receipt,
            active,
            agent_name,
            reopen=event,
        )
        immutable_json(
            _lane_recovery_receipt_path(active, private_root, agent_name),
            receipt,
        )
        immutable_json(
            _version_reopen_pointer_path(active, private_root, agent_name),
            {
                "schema_version": "1.0.0",
                "event_path": event_path.relative_to(
                    daily_runtime_root(private_root)
                ).as_posix(),
                "event_digest": event["event_digest"],
            },
        )
    return {
        **_status(active, private_root),
        "agent": agent_name,
        "issue": issue_id,
        "status": "reclassified",
        "endpoint_requests": 0,
        "insight_runs": 0,
    }


def reclassify_daily_card_linkage(
    *,
    confirmed: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("Daily card linkage reclassification requires --confirm")
    private_root = (base or runtime_root()).resolve()
    active = _read_active(private_root, allowed_states={"TRAFFIC"})
    batch_path = _run_root(active, private_root) / "card-linkage-reclassification.json"
    if batch_path.is_file():
        value = read_json(batch_path)
        if value.get("batch_digest") != content_hash(
            {key: item for key, item in value.items() if key != "batch_digest"}
        ):
            raise ContractError("Daily card linkage batch receipt is invalid")
        return {
            **_status(active, private_root),
            "status": "already_complete",
            "changed_issue_ids": value["changed_issue_ids"],
            "unchanged_issue_ids": value["unchanged_issue_ids"],
            "endpoint_requests": 0,
            "insight_runs": 0,
        }
    records = {
        agent_name: _effective_lane_receipt(active, private_root, agent_name)[1]
        for agent_name in AGENT_ORDER
    }
    issue_results = [
        (agent_name, item)
        for agent_name, receipt in records.items()
        for item in _agent_result(receipt["result"]).issues
    ]
    if (
        len(issue_results) != 20
        or {item.logical_version for _, item in issue_results}
        != {
            issue_id
            for issue_ids in active.value["bindings"]["selection"].values()
            for issue_id in issue_ids
        }
    ):
        raise ContractError("Daily card linkage batch does not cover 20 issues")
    changed: list[str] = []
    unchanged: list[str] = []
    reasons: dict[str, str] = {}
    for agent_name, item in issue_results:
        issue_id = item.logical_version
        if (
            item.status == "not_at_bar"
            and item.error_code == "insufficient_trace_evidence"
        ):
            _reclassify_completed_daily_version(
                agent_name,
                issue_id,
                confirmed=True,
                base=private_root,
            )
            changed.append(issue_id)
            reasons[issue_id] = "card_linkage_threshold_corrected"
        else:
            unchanged.append(issue_id)
            reasons[issue_id] = "classification_unchanged"
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-card-linkage-reclassification",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "issue_ids": sorted(item.logical_version for _, item in issue_results),
        "changed_issue_ids": sorted(changed),
        "unchanged_issue_ids": sorted(unchanged),
        "reasons": reasons,
        "endpoint_requests": 0,
        "insight_runs": 0,
        "batch_digest": "",
    }
    value["batch_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "batch_digest"}
    )
    immutable_json(batch_path, value)
    return {
        **_status(active, private_root),
        "status": "reclassified",
        "changed_issue_ids": value["changed_issue_ids"],
        "unchanged_issue_ids": value["unchanged_issue_ids"],
        "endpoint_requests": 0,
        "insight_runs": 0,
    }


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
                "command": _pending_lane_command(
                    active,
                    private_root,
                    agent_name,
                ),
                **(
                    {"recovery": status["lane_recovery"][agent_name]}
                    if agent_name in status["lane_recovery"]
                    else {}
                ),
            }
            for agent_name in AGENT_ORDER
            if agent_name in status["pending_agent_lanes"]
        ]
        guide["next"] = (
            "Start one visible Copilot sub session per pending Agent lane; "
            "do not split versions or start subprocess workers."
        )
        if any(
            value["status"] in {"recovery_blocked", "recovery_exhausted"}
            for value in status["lane_recovery"].values()
        ):
            guide["next"] = (
                "The Daily run has an unrecoverable Agent lane; run daily-fail "
                "centrally with a public-safe reason code and --confirm."
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


def _pending_lane_command(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> str:
    version = _read_version_reopen(active, base, agent_name)
    if version is not None:
        return (
            "python -m agent_insights_quality daily-run-reopened-version "
            f"--agent {agent_name} --issue {version['issue_id']}"
        )
    return (
        "python -m agent_insights_quality daily-run-agent "
        f"--agent {agent_name}"
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
    worker_claim: Mapping[str, Any] | None = None,
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
        or current_bindings["checkout_commit_sha"]
        != expected_bindings["checkout_commit_sha"]
        or current_bindings["run_contract_digest"]
        != expected_bindings["run_contract_digest"]
        or (
            worker_claim is not None
            and not _reopened_lane_worker_claim_is_current(
                current,
                base,
                worker_claim,
            )
        )
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
        "schemas/daily-agent-recovery-receipt.schema.json",
        "schemas/daily-lane-recovery.schema.json",
        "schemas/daily-lane-reopen.schema.json",
        "schemas/daily-lane-worker-claim.schema.json",
        "schemas/daily-version-reopen.schema.json",
        "schemas/daily-email-test-preview.schema.json",
        "schemas/prompt-traffic.schema.json",
        "schemas/assessment-package.schema.json",
        "src/agent_insights_quality/prompts/assessment.md",
    ):
        runtime_files[relative] = file_hash(ROOT / relative)
    return content_hash(
        {
            "schema_version": "5.0.0",
            "repository": bindings["repository"],
            "checkout_commit_sha": bindings["checkout_commit_sha"],
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


def _assert_checkout_binding(
    active: DailyRecord,
    base: Path,
    *,
    agent_name: str | None = None,
    allow_recovery_commit: bool = False,
    worker_claim: Mapping[str, Any] | None = None,
) -> None:
    agents, issues = load_catalogs()
    bindings = active.value["bindings"]
    commit_sha = current_clean_commit()
    if catalog_hashes(agents, issues) != bindings["catalog_hashes"]:
        raise ContractError("Stale Daily worker is fenced from the active lifecycle")
    if commit_sha == bindings["checkout_commit_sha"]:
        return
    if (
        worker_claim is not None
        and worker_claim["recovery_commit_sha"] == commit_sha
        and worker_claim["recovery_verifier_digest"]
        == _lane_recovery_verifier_digest()
        and _reopened_lane_worker_claim_is_current(active, base, worker_claim)
    ):
        return
    candidates = (
        [agent_name]
        if agent_name is not None
        else list(AGENT_ORDER)
        if allow_recovery_commit
        else []
    )
    for candidate in candidates:
        claim = _read_lane_worker_claim(active, base, candidate)
        if (
            claim is not None
            and claim["recovery_commit_sha"] == commit_sha
            and claim["recovery_verifier_digest"]
            == _lane_recovery_verifier_digest()
        ):
            return
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


def _lane_recovery_path(active: DailyRecord, base: Path, agent_name: str) -> Path:
    return _lane_root(active, base, agent_name) / "recovery-state.json"


def _lane_reopen_pointer_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> Path:
    return _lane_root(active, base, agent_name) / "reopen-active.json"


def _lane_reopen_event_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
    digest: str,
) -> Path:
    return (
        _lane_root(active, base, agent_name)
        / "reopen-events"
        / f"{digest.removeprefix('sha256:')}.json"
    )


def _version_reopen_pointer_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> Path:
    return _lane_root(active, base, agent_name) / "version-reopen-active.json"


def _version_reopen_event_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
    digest: str,
) -> Path:
    return (
        _lane_root(active, base, agent_name)
        / "version-reopen-events"
        / f"{digest.removeprefix('sha256:')}.json"
    )


def _lane_recovery_receipt_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> Path:
    return _lane_root(active, base, agent_name) / "recovery-completion-receipt.json"


def _lane_worker_claim_pointer_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> Path:
    return _lane_root(active, base, agent_name) / "reopen-worker-active.json"


def _lane_worker_claim_event_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
    digest: str,
) -> Path:
    return (
        _lane_root(active, base, agent_name)
        / "worker-claims"
        / f"{digest.removeprefix('sha256:')}.json"
    )


def _claim_reopened_lane_worker(
    active: DailyRecord,
    base: Path,
    agent_name: str,
    reopen: Mapping[str, Any],
) -> dict[str, Any]:
    lock = _coordinator_lock(base)
    with lock:
        current = DailyLifecycle(lock=lock, base=base).read_active()
        if (
            current.value["state"] != "TRAFFIC"
            or current.value["execution_id"] != active.value["execution_id"]
            or _read_active_reopen(current, base, agent_name) != reopen
            or _lane_recovery_receipt_path(current, base, agent_name).exists()
        ):
            raise ContractError("Reopened Daily lane is no longer claimable")
        pointer_path = _lane_worker_claim_pointer_path(current, base, agent_name)
        worker_reference = content_hash(
            {"worktree": str(ROOT.resolve()).casefold()}
        )
        if pointer_path.exists():
            existing = _read_lane_worker_claim(current, base, agent_name)
            if (
                existing is not None
                and existing["worker_reference"] == worker_reference
                and existing["recovery_commit_sha"] == current_clean_commit()
                and existing["recovery_verifier_digest"]
                == _lane_recovery_verifier_digest()
            ):
                return existing
            raise ContractError("Reopened Daily lane already has an active worker")
        original = _read_lane_receipt(
            _lane_receipt_path(current, base, agent_name),
            current,
            agent_name,
        )
        result = _agent_result(original["result"])
        checkpoint_store = _checkpoint_store(current, base)
        if reopen["kind"] == "daily-lane-reopen":
            if any(
                item.status != "skipped_baseline"
                or item.operation_ids
                or item.endpoint_request_count
                or checkpoint_store.has_version_progress(
                    agent_name,
                    item.logical_version,
                )
                for item in result.issues
            ):
                raise ContractError("Reopened Daily lane has issue traffic or progress")
        else:
            target = next(
                item
                for item in result.issues
                if item.logical_version == reopen["issue_id"]
            )
            if target.operation_ids or target.endpoint_request_count:
                raise ContractError("Reopened Daily issue already recorded traffic")
        commit_sha = current_clean_commit()
        agents, issues = load_catalogs()
        if catalog_hashes(agents, issues) != current.value["bindings"]["catalog_hashes"]:
            raise ContractError("Reopened Daily worker content binding changed")
        claim = {
            "schema_version": "1.0.0",
            "kind": "daily-lane-worker-claim",
            "execution_id": current.value["execution_id"],
            "run_contract_digest": current.value["bindings"]["run_contract_digest"],
            "agent_name": agent_name,
            "reopen_event_digest": reopen["event_digest"],
            "epoch": 1,
            "recovery_commit_sha": commit_sha,
            "recovery_verifier_digest": _lane_recovery_verifier_digest(),
            "worker_reference": worker_reference,
            "claim_nonce": uuid.uuid4().hex,
            "claim_digest": "",
        }
        claim["claim_digest"] = content_hash(
            {key: item for key, item in claim.items() if key != "claim_digest"}
        )
        _validate_lane_worker_claim(claim, current, reopen)
        event_path = _lane_worker_claim_event_path(
            current,
            base,
            agent_name,
            claim["claim_digest"],
        )
        immutable_json(event_path, claim)
        atomic_json(
            pointer_path,
            {
                "schema_version": "1.0.0",
                "epoch": claim["epoch"],
                "claim_path": event_path.relative_to(
                    daily_runtime_root(base)
                ).as_posix(),
                "claim_digest": claim["claim_digest"],
            },
        )
        return claim


def _read_lane_worker_claim(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> dict[str, Any] | None:
    pointer_path = _lane_worker_claim_pointer_path(active, base, agent_name)
    if not pointer_path.is_file():
        return None
    pointer = read_json(pointer_path)
    if set(pointer) != {
        "schema_version",
        "epoch",
        "claim_path",
        "claim_digest",
    } or pointer["schema_version"] != "1.0.0":
        raise ContractError("Daily lane worker claim pointer is invalid")
    claim_path = (daily_runtime_root(base) / pointer["claim_path"]).resolve()
    expected_path = _lane_worker_claim_event_path(
        active,
        base,
        agent_name,
        pointer["claim_digest"],
    ).resolve()
    if claim_path != expected_path or not claim_path.is_file():
        raise ContractError("Daily lane worker claim is orphaned")
    claim = read_json(claim_path)
    reopen = _read_active_reopen(active, base, agent_name)
    if reopen is None:
        raise ContractError("Daily lane worker claim lacks its reopen event")
    _validate_lane_worker_claim(claim, active, reopen)
    if (
        pointer["epoch"] != claim["epoch"]
        or pointer["claim_digest"] != claim["claim_digest"]
    ):
        raise ContractError("Daily lane worker claim pointer is stale")
    return claim


def _validate_lane_worker_claim(
    value: Mapping[str, Any],
    active: DailyRecord,
    reopen: Mapping[str, Any],
) -> None:
    errors = list(
        Draft202012Validator(read_json(_WORKER_CLAIM_SCHEMA)).iter_errors(value)
    )
    if errors:
        raise ContractError(
            f"Daily lane worker claim is invalid: {errors[0].message}"
        )
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"]
        != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != reopen["agent_name"]
        or value["reopen_event_digest"] != reopen["event_digest"]
        or value["claim_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "claim_digest"}
        )
    ):
        raise ContractError("Daily lane worker claim binding is stale")


def _reopened_lane_worker_claim_is_current(
    active: DailyRecord,
    base: Path,
    claim: Mapping[str, Any],
) -> bool:
    current = _read_lane_worker_claim(active, base, str(claim["agent_name"]))
    return current == claim


def _lane_recovery_verifier_digest() -> str:
    return content_hash(
        {
            path.relative_to(ROOT).as_posix(): file_hash(path)
            for path in (
                ROOT / "src" / "agent_insights_quality" / "baseline_policy.py",
                ROOT / "src" / "agent_insights_quality" / "assessment.py",
                ROOT / "src" / "agent_insights_quality" / "daily_coordinator.py",
                ROOT / "src" / "agent_insights_quality" / "models.py",
                ROOT / "src" / "agent_insights_quality" / "runner.py",
                ROOT / "src" / "agent_insights_quality" / "runtime_state.py",
                ROOT / "schemas" / "daily-lane-reopen.schema.json",
                ROOT / "schemas" / "daily-agent-recovery-receipt.schema.json",
                ROOT / "schemas" / "daily-lane-worker-claim.schema.json",
                ROOT / "schemas" / "daily-version-reopen.schema.json",
            )
        }
    )


def _read_lane_reopen(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> dict[str, Any] | None:
    pointer_path = _lane_reopen_pointer_path(active, base, agent_name)
    if not pointer_path.is_file():
        return None
    pointer = read_json(pointer_path)
    if set(pointer) != {"schema_version", "event_path", "event_digest"} or pointer[
        "schema_version"
    ] != "1.0.0":
        raise ContractError("Daily lane reopen pointer is invalid")
    event_path = (daily_runtime_root(base) / pointer["event_path"]).resolve()
    expected_path = _lane_reopen_event_path(
        active,
        base,
        agent_name,
        pointer["event_digest"],
    ).resolve()
    if event_path != expected_path or not event_path.is_file():
        raise ContractError("Daily lane reopen event is orphaned")
    event = read_json(event_path)
    _validate_lane_reopen(event, active, agent_name)
    if event["event_digest"] != pointer["event_digest"]:
        raise ContractError("Daily lane reopen pointer is stale")
    return event


def _read_version_reopen(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> dict[str, Any] | None:
    pointer_path = _version_reopen_pointer_path(active, base, agent_name)
    if not pointer_path.is_file():
        return None
    pointer = read_json(pointer_path)
    if set(pointer) != {"schema_version", "event_path", "event_digest"} or pointer[
        "schema_version"
    ] != "1.0.0":
        raise ContractError("Daily version reopen pointer is invalid")
    event_path = (daily_runtime_root(base) / pointer["event_path"]).resolve()
    expected_path = _version_reopen_event_path(
        active,
        base,
        agent_name,
        pointer["event_digest"],
    ).resolve()
    if event_path != expected_path or not event_path.is_file():
        raise ContractError("Daily version reopen event is orphaned")
    event = read_json(event_path)
    _validate_version_reopen(
        event,
        active,
        agent_name,
        str(event.get("issue_id") or ""),
    )
    if event["event_digest"] != pointer["event_digest"]:
        raise ContractError("Daily version reopen pointer is stale")
    return event


def _read_active_reopen(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> dict[str, Any] | None:
    lane = _read_lane_reopen(active, base, agent_name)
    version = _read_version_reopen(active, base, agent_name)
    if lane is not None and version is not None:
        raise ContractError("Daily Agent has conflicting reopen events")
    return lane or version


def _validate_version_reopen(
    value: Mapping[str, Any],
    active: DailyRecord,
    agent_name: str,
    issue_id: str,
) -> None:
    errors = list(
        Draft202012Validator(read_json(_VERSION_REOPEN_SCHEMA)).iter_errors(value)
    )
    if errors:
        raise ContractError(
            f"Daily version reopen event is invalid: {errors[0].message}"
        )
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"]
        != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != agent_name
        or value["issue_id"] != issue_id
        or issue_id not in active.value["bindings"]["selection"][agent_name]
        or value["original_checkout_commit_sha"]
        != active.value["bindings"]["checkout_commit_sha"]
        or value["catalog_hashes"] != active.value["bindings"]["catalog_hashes"]
        or value["registry_digest"]
        != active.value["bindings"]["registry"]["content_digest"]
        or value["event_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "event_digest"}
        )
    ):
        raise ContractError("Daily version reopen event binding is stale")


def _validate_lane_reopen(
    value: Mapping[str, Any],
    active: DailyRecord,
    agent_name: str,
) -> None:
    errors = list(Draft202012Validator(read_json(_REOPEN_SCHEMA)).iter_errors(value))
    if errors:
        raise ContractError(f"Daily lane reopen event is invalid: {errors[0].message}")
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"]
        != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != agent_name
        or value["assigned_issue_ids"]
        != active.value["bindings"]["selection"][agent_name]
        or value["original_checkout_commit_sha"]
        != active.value["bindings"]["checkout_commit_sha"]
        or value["catalog_hashes"] != active.value["bindings"]["catalog_hashes"]
        or value["registry_digest"]
        != active.value["bindings"]["registry"]["content_digest"]
        or value["event_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "event_digest"}
        )
    ):
        raise ContractError("Daily lane reopen event binding is stale")


def _lane_recovery_state(
    active: DailyRecord,
    agent_name: str,
    result: AgentResult,
    checkpoint_store: VersionCheckpointStore,
    maximum: int,
) -> dict[str, Any] | None:
    reason_code = result.baseline.error_code
    if reason_code is None:
        reason_code = next(
            (
                item.error_code
                for item in result.issues
                if item.error_code == "insight_run_start_unresolved"
            ),
            None,
        )
    if reason_code not in {
        "baseline_evidence_incomplete",
        "baseline_recovery_prepare_failed",
        "baseline_recovery_blocked",
        "baseline_recovery_exhausted",
        "insight_run_start_unresolved",
    }:
        return None
    claimed = checkpoint_store.agent_recovery_count(agent_name, maximum)
    remaining = maximum - claimed
    if reason_code == "insight_run_start_unresolved":
        status = "recovery_pending"
    elif (
        reason_code == "baseline_recovery_blocked"
        or checkpoint_store.has_unresolved_insight_state()
    ):
        status = "recovery_blocked"
        reason_code = "baseline_recovery_blocked"
    elif reason_code == "baseline_recovery_exhausted" or remaining == 0:
        status = "recovery_exhausted"
    else:
        status = "recovery_pending"
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-lane-recovery",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": agent_name,
        "status": status,
        "reason_code": reason_code,
        "recoveries_claimed": claimed,
        "maximum_recoveries": maximum,
        "remaining_recoveries": remaining,
        "state_digest": "",
    }
    value["state_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "state_digest"}
    )
    _validate_lane_recovery(value, active, agent_name)
    return value


def _read_lane_recovery(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> dict[str, Any] | None:
    path = _lane_recovery_path(active, base, agent_name)
    if not path.is_file():
        return None
    value = read_json(path)
    _validate_lane_recovery(value, active, agent_name)
    return value


def _validate_lane_recovery(
    value: Mapping[str, Any],
    active: DailyRecord,
    agent_name: str,
) -> None:
    errors = list(
        Draft202012Validator(read_json(_RECOVERY_SCHEMA)).iter_errors(value)
    )
    if errors:
        raise ContractError(
            f"Daily Agent lane recovery state is invalid: {errors[0].message}"
        )
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"]
        != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != agent_name
        or value["recoveries_claimed"] + value["remaining_recoveries"]
        != value["maximum_recoveries"]
        or value["state_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "state_digest"}
        )
    ):
        raise ContractError("Stale Daily Agent lane recovery state is fenced")


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


def _stamp_lane_recovery_receipt(
    active: DailyRecord,
    agent_name: str,
    result: AgentResult,
    reopen: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-agent-lane-recovery-receipt",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": agent_name,
        "issue_ids": active.value["bindings"]["selection"][agent_name],
        "supersedes_receipt_digest": reopen["original_receipt_digest"],
        "reopen_event_digest": reopen["event_digest"],
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
    *,
    reopen: Mapping[str, Any] | None = None,
) -> None:
    schema = _RECOVERY_RECEIPT_SCHEMA if reopen is not None else _RECEIPT_SCHEMA
    errors = list(
        Draft202012Validator(read_json(schema)).iter_errors(value)
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
    if reopen is not None and (
        value["supersedes_receipt_digest"] != reopen["original_receipt_digest"]
        or value["reopen_event_digest"] != reopen["event_digest"]
        or value["receipt_digest"] == value["supersedes_receipt_digest"]
        or content_hash(value["result"]["baseline"])
        != reopen["accepted_baseline_digest"]
    ):
        raise ContractError("Daily Agent lane recovery ancestry is invalid")
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
    *,
    reopen: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"{agent_name} Daily Agent lane receipt is missing")
    value = read_json(path)
    _validate_lane_receipt(value, active, agent_name, reopen=reopen)
    return value


def _effective_lane_receipt(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> tuple[Path, dict[str, Any]]:
    reopen = _read_active_reopen(active, base, agent_name)
    path = (
        _lane_recovery_receipt_path(active, base, agent_name)
        if reopen is not None
        else _lane_receipt_path(active, base, agent_name)
    )
    return path, _read_lane_receipt(
        path,
        active,
        agent_name,
        reopen=reopen,
    )


def _effective_lane_receipt_exists(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> bool:
    reopen = _read_active_reopen(active, base, agent_name)
    return (
        _lane_recovery_receipt_path(active, base, agent_name).is_file()
        if reopen is not None
        else _lane_receipt_path(active, base, agent_name).is_file()
    )


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
        issue_trace_gap_acceptance=value.get(
            "issue_trace_gap_acceptance"
        ),
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
                        evidence_sufficient=bool(
                            result["evidence_sufficient"]
                        ),
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
                        evidence_sufficient=bool(
                            result["evidence_sufficient"]
                        ),
                    )
                    for result in item["trace_assertion_results"]
                ),
                error_code=item.get("error_code"),
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
        if _effective_lane_receipt_exists(active, base, agent_name)
    ]
    pending = [agent_name for agent_name in AGENT_ORDER if agent_name not in completed]
    lane_recovery = {
        agent_name: recovery
        for agent_name in pending
        if (recovery := _read_lane_recovery(active, base, agent_name)) is not None
    }
    return {
        "state": active.value["state"],
        "report_date": active.value["bindings"]["report_date"],
        "delivery_mode": active.value["bindings"]["delivery_mode"],
        "publish_preview": active.value["bindings"]["publish_preview"],
        "completed_agent_lanes": completed,
        "pending_agent_lanes": pending,
        "lane_recovery": lane_recovery,
        "package_target": 25,
        "adx_publication_status": active.value["artifacts"][
            "adx_publication_status"
        ],
    }


def _rerun(public_run_id: str) -> int:
    marker = public_run_id.rpartition("-r")
    return int(marker[2]) if marker[1] else 0
