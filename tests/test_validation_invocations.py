from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    file_hash,
    read_json,
)
from agent_insights_quality.validation_coordinator import (
    _invocation_receipts_for_verification,
)
from agent_insights_quality.validation_invocations import (
    assert_invocation_receipt_set_isolated,
    _locate_legacy_source_archive,
    _read_legacy_shard_artifact,
    _resources_for_invocation,
    _validate_migration_authority_coverage,
    extract_legacy_shard_invocations,
    load_invocation_receipt,
    recover_supplemental_legacy_invocations,
    select_reusable_invocation_receipts,
    write_invocation_receipt,
)
from agent_insights_quality.validation_lifecycle import LocalValidationLock
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_validation_plan,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_runtime import DeployedRuntime

HEAD = "a" * 40
NEXT_HEAD = "b" * 40
RUN_ID = "validation-0123456789ab"


def _context() -> tuple[dict, dict, list, dict[str, DeployedRuntime]]:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    authorities = authority_specs(agents, issues)
    plan = prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha=HEAD,
        local_run_id="synthetic-invocations",
    )
    plan["run_id"] = RUN_ID
    runtimes = {
        authority.authority_id: DeployedRuntime(
            authority_id=authority.authority_id,
            runtime_kind=authority.runtime_kind,
            runtime_agent_name=(
                f"{authority.canonical_agent}-"
                f"{'baseline' if authority.authority_kind == 'baseline' else authority.authority_id}"
            ),
            runtime_agent_version=f"server-{index}",
            provider_agent_id=f"agent-{index}",
            provider_agent_version_id=f"version-{index}",
            provider_content_digest=authority.source_content_digest,
            hosted_identity_id=(
                None
                if authority.runtime_kind == "prompt"
                else f"identity-{index}"
            ),
            hosted_blueprint_id=(
                None
                if authority.runtime_kind == "prompt"
                else f"blueprint-{index}"
            ),
            hosted_deployment_id=(
                None
                if authority.runtime_kind == "prompt"
                else f"deployment-{index}"
            ),
            runtime_principal_id=(
                None
                if authority.runtime_kind == "prompt"
                else f"principal-{index}"
            ),
            telemetry_identity_id=f"telemetry-{index}",
            connection_ids=(f"connection-{index}",),
        )
        for index, authority in enumerate(authorities, start=1)
    }
    prepared = {
        "repository": plan["repository"],
        "pr_number": plan["pr_number"],
        "commit_sha": HEAD,
        "run_id": RUN_ID,
        "journal_digest": content_hash({"lifecycle": RUN_ID}),
        "desired_state_reference": {
            "path": "desired-state/synthetic.json",
            "digest": content_hash({"desired": RUN_ID}),
        },
        "digests": {
            "validation_digest": plan["validation_digest"],
            "shared_validation_digest": plan["shared_validation_digest"],
            "verifier_digest": plan["verifier_digest"],
            "invocation_contract_digest": plan[
                "invocation_contract_digest"
            ],
            "execution_matrix_digest": plan["execution_matrix_digest"],
            "runtime_topology_digest": content_hash(
                [
                    {
                        **asdict(runtime),
                        "connection_ids": list(runtime.connection_ids),
                    }
                    for runtime in runtimes.values()
                ]
            ),
            "quota_plan_digest": content_hash({"capacity": 8}),
        },
        "project": {
            "name": plan["project_name"],
            "provider_id": "synthetic-project",
        },
        "substrate": {
            "telemetry_resource_id": "/synthetic/telemetry",
        },
        "runtime_topology": {
            "telemetry_resource_set": plan["telemetry_resource_set"],
            "agents": [
                {
                    **asdict(runtime),
                    "canonical_agent": authority.canonical_agent,
                    "logical_version": authority.logical_version,
                    "connection_ids": list(runtime.connection_ids),
                }
                for authority, runtime in zip(
                    authorities,
                    runtimes.values(),
                    strict=True,
                )
            ],
        },
    }
    return prepared, plan, authorities, runtimes


def _invocation(authority, *, qualifier: str = "one") -> dict:
    hosted = authority.runtime_kind in {
        "hosted_code",
        "hosted_custom_container",
    }
    scenarios = []
    for scenario in authority.validation_rules["scenarios"]:
        scenarios.append(
            {
                "scenario_id": scenario["id"],
                "issue_invocations": [
                    _attempt_invocation(
                        attempt,
                        qualifier=f"{qualifier}-issue-{attempt['index']}",
                        hosted=hosted,
                    )
                    for attempt in scenario["attempts"]
                ],
                "v0_invocations": (
                    []
                    if authority.authority_kind == "baseline"
                    else [
                        _attempt_invocation(
                            attempt,
                            qualifier=f"{qualifier}-v0-{attempt['index']}",
                            hosted=hosted,
                        )
                        for attempt in scenario["attempts"]
                    ]
                ),
            }
        )
    return {
        "authority_id": authority.authority_id,
        "scenarios": scenarios,
    }


def _attempt_invocation(
    attempt,
    *,
    qualifier: str,
    hosted: bool,
) -> dict:
    count = len(attempt["setup_steps"]) + len(attempt["probe_steps"])
    started = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    return {
        "started_at": started.isoformat(),
        "completed_at": (started + timedelta(seconds=1)).isoformat(),
        "response_ids": [
            f"response-{qualifier}-{index}" for index in range(1, count + 1)
        ],
        "usable_results": [True] * count,
        "session_id": f"session-{qualifier}" if hosted else None,
    }


def _write_receipt(
    *,
    root: Path,
    prepared: dict,
    plan: dict,
    shard_id: int = 1,
    authority,
    authorities: list,
    runtimes: dict[str, DeployedRuntime],
    qualifier: str = "one",
    migrated_from: dict[str, str] | None = None,
) -> dict[str, str]:
    invocation = _invocation(authority, qualifier=qualifier)
    return write_invocation_receipt(
        prepared=prepared,
        plan=plan,
        shard_id=shard_id,
        authority=authority,
        runtime=runtimes[authority.authority_id],
        paired_v0_authority=(
            None
            if authority.authority_kind == "baseline"
            else next(
                item
                for item in authorities
                if item.authority_id == f"{authority.canonical_agent}/v0"
            )
        ),
        paired_v0_runtime=(
            None
            if authority.authority_kind == "baseline"
            else runtimes[f"{authority.canonical_agent}/v0"]
        ),
        invocation=invocation,
        resources=_resources(invocation, authority, runtimes),
        fence=lambda: None,
        root=root,
        migrated_from=migrated_from,
    )


def _resources(
    invocation: dict,
    authority,
    runtimes: dict[str, DeployedRuntime],
) -> list[dict]:
    resources = []
    paired_id = f"{authority.canonical_agent}/v0"
    for scenario in invocation["scenarios"]:
        expected_scenario = next(
            item
            for item in authority.validation_rules["scenarios"]
            if item["id"] == scenario["scenario_id"]
        )
        for role, key in (
            (
                "baseline"
                if authority.authority_kind == "baseline"
                else "issue",
                "issue_invocations",
            ),
            ("paired_v0", "v0_invocations"),
        ):
            if role == "paired_v0" and authority.authority_kind == "baseline":
                continue
            target_id = (
                paired_id
                if role == "paired_v0"
                else authority.authority_id
            )
            for expected_attempt, attempt in zip(
                expected_scenario["attempts"],
                scenario[key],
                strict=True,
            ):
                scope = {
                    "executing_authority_id": authority.authority_id,
                    "target_authority_id": target_id,
                    "conversation_role": role,
                    "scenario_id": scenario["scenario_id"],
                    "conversation_group": expected_attempt[
                        "conversation_group"
                    ],
                    "attempt": expected_attempt["index"],
                }
                providers = []
                if attempt["session_id"]:
                    providers.append(
                        (
                            "session",
                            attempt["session_id"],
                            content_hash(
                                {
                                    "authority_id": target_id,
                                    "kind": "session",
                                    "execution_scope": scope,
                                }
                            ),
                        )
                    )
                providers.extend(
                    (
                        "stored_response",
                        item,
                        content_hash(
                            {
                                "authority_id": target_id,
                                "kind": "stored_response",
                                "execution_scope": scope,
                                "step": index,
                            }
                        ),
                    )
                    for index, item in enumerate(
                        attempt["response_ids"],
                        start=1,
                    )
                )
                for kind, provider_id, intent in providers:
                    base = {
                        "kind": kind,
                        "intent_reference": intent,
                        "deterministic_name": provider_id,
                        "authority_id": target_id,
                        "parent_id": runtimes[target_id].provider_agent_id,
                    }
                    resources.append(
                        {
                            **base,
                            "state": "create_intent",
                            "runtime_kind": authority.runtime_kind,
                            "discovery_key": intent,
                        }
                    )
                    resources.append(
                        {
                            **base,
                            "state": "created",
                            "provider_id": provider_id,
                        }
                    )
    return resources


def _write_complete_legacy_generation(
    root: Path,
    *,
    prepared: dict,
    authorities: list,
    runtimes: dict[str, DeployedRuntime],
) -> tuple[Path, dict, list[dict]]:
    authority_ids = [item.authority_id for item in authorities]
    assignments = [
        {
            "shard_id": index + 1,
            "authority_ids": authority_ids[index::10],
        }
        for index in range(10)
    ]
    desired = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-desired-state",
        "run_id": RUN_ID,
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "authorities": [
            {
                "authority_id": authority.authority_id,
                "source_content_digest": authority.source_content_digest,
                "provider_content_digest": runtimes[
                    authority.authority_id
                ].provider_content_digest,
            }
            for authority in authorities
        ],
        "desired_state_digest": "",
    }
    desired["desired_state_digest"] = _digest_without(
        desired,
        "desired_state_digest",
    )
    desired_path = root / "desired-state" / "legacy.json"
    atomic_json(desired_path, desired)
    active = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-lifecycle",
        "state": "VALIDATING",
        **prepared,
        "validation_authority_ids": authority_ids,
        "failure": None,
        "deployment": {"failures": []},
        "shard_assignments": assignments,
        "desired_state_reference": {
            "path": desired_path.relative_to(root).as_posix(),
            "digest": desired["desired_state_digest"],
        },
        "journal_digest": "",
    }
    active["journal_digest"] = _digest_without(active, "journal_digest")
    archive_path = root / "legacy-active.json"
    atomic_json(archive_path, active)
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    by_id = {item.authority_id: item for item in authorities}
    for assignment in assignments:
        invocations = [
            _invocation(by_id[authority_id], qualifier=authority_id)
            for authority_id in assignment["authority_ids"]
        ]
        invocations.sort(key=lambda item: item["authority_id"])
        artifact = {
            "schema_version": "2.0.0",
            "kind": "test-agent-validation-shard-invocations",
            "shard_id": assignment["shard_id"],
            "authority_ids": assignment["authority_ids"],
            "binding": _legacy_binding(
                active,
                assignment["authority_ids"],
                runtime_by_id,
            ),
            "status": "invoked",
            "resources": [
                resource
                for invocation in invocations
                for resource in _resources(
                    invocation,
                    by_id[invocation["authority_id"]],
                    runtimes,
                )
                if not (
                    by_id[invocation["authority_id"]].runtime_kind != "prompt"
                    and resource["kind"] == "stored_response"
                )
            ],
            "invocations": invocations,
            "artifact_digest": "",
        }
        artifact["artifact_digest"] = _digest_without(
            artifact,
            "artifact_digest",
        )
        artifact_path = (
            root
            / "shards"
            / "ninghu"
            / "agent-insights-quality"
            / "999"
            / RUN_ID
            / f"shard-{assignment['shard_id']:02d}"
            / "invocations.json"
        )
        atomic_json(artifact_path, artifact)
    return archive_path, active, assignments


def _write_completed_supplemental_replay_state(
    root: Path,
    *,
    mixed_receipt: bool = False,
    cross_pr_receipt: bool = False,
) -> tuple[dict, dict, list, list[Path], dict, dict]:
    prepared, plan, authorities, runtimes = _context()
    authority_ids = [item.authority_id for item in authorities]
    imported = authority_ids[:28]
    incomplete = authority_ids[28:]
    receipt_paths = []
    for index, authority in enumerate(authorities):
        receipt_prepared = prepared
        receipt_plan = plan
        if mixed_receipt and index == 0:
            receipt_prepared = copy.deepcopy(prepared)
            receipt_prepared["journal_digest"] = content_hash(
                {"lifecycle": "other-source"}
            )
        if cross_pr_receipt and index == 0:
            receipt_prepared = copy.deepcopy(prepared)
            receipt_prepared["pr_number"] += 1
            receipt_plan = copy.deepcopy(plan)
            receipt_plan["pr_number"] += 1
        reference = _write_receipt(
            root=root,
            prepared=receipt_prepared,
            plan=receipt_plan,
            authority=authority,
            authorities=authorities,
            runtimes=runtimes,
            qualifier=f"supplemental-{authority.authority_id}",
            migrated_from={
                "schema_version": "2.0.0",
                "kind": "test-agent-validation-shard-invocations",
                "artifact_digest": content_hash(
                    {"authority_id": authority.authority_id}
                ),
            },
        )
        receipt_path = root / reference["path"]
        if cross_pr_receipt and index == 0:
            destination = (
                root
                / "invocation-receipts"
                / prepared["repository"].replace("/", "--")
                / str(prepared["pr_number"])
                / prepared["run_id"]
                / authority.authority_id.replace("/", "--")
                / receipt_path.name
            )
            destination.parent.mkdir(parents=True)
            receipt_path.replace(destination)
            receipt_path = destination
        receipt_paths.append(receipt_path)
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": prepared["journal_digest"],
        "imported_authority_ids": imported,
        "incomplete_authority_ids": incomplete,
        "migration_digest": "",
    }
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    atomic_json(
        root
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json",
        marker,
    )
    supplemental = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1-supplemental",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": prepared["journal_digest"],
        "source_marker_digest": marker["migration_digest"],
        "source_archive_digest": content_hash({"archive": "not-retained"}),
        "imported_authority_ids": incomplete,
        "incomplete_authority_ids": [],
        "migration_digest": "",
    }
    supplemental["migration_digest"] = _digest_without(
        supplemental,
        "migration_digest",
    )
    atomic_json(
        root
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1-supplemental.json",
        supplemental,
    )
    return (
        prepared,
        plan,
        authorities,
        receipt_paths,
        marker,
        supplemental,
    )


def test_all_current_receipts_select_zero_invoke_and_41_verify(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    for authority in authorities:
        _write_receipt(
            root=tmp_path,
            prepared=prepared,
            plan=plan,
            authority=authority,
            authorities=authorities,
            runtimes=runtimes,
        )

    verify_ids = [item.authority_id for item in authorities]
    invoke_ids, reused = select_reusable_invocation_receipts(
        authorities=authorities,
        authority_ids=verify_ids,
        runtime_topology=prepared["runtime_topology"],
        prepared=prepared,
        plan=plan,
        root=tmp_path,
    )

    assert invoke_ids == []
    assert len(reused) == len(verify_ids) == 41
    first = load_invocation_receipt(reused[0], root=tmp_path)
    assert first["origin_binding"]["quota_plan_digest"] == prepared["digests"][
        "quota_plan_digest"
    ]


def test_reused_invocation_sends_no_traffic_and_binds_current_verifier(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    issue = next(
        item for item in authorities if item.authority_id == "issue-020"
    )
    reference = _write_receipt(
        root=tmp_path,
        prepared=prepared,
        plan=plan,
        authority=issue,
        authorities=authorities,
        runtimes=runtimes,
    )
    current = copy.deepcopy(prepared)
    current["commit_sha"] = NEXT_HEAD
    current["digests"]["shared_validation_digest"] = content_hash(
        {"verifier": NEXT_HEAD}
    )
    current_plan = copy.deepcopy(plan)
    current_plan["commit_sha"] = NEXT_HEAD
    current_plan["shared_validation_digest"] = current["digests"][
        "shared_validation_digest"
    ]
    invoked, reused = select_reusable_invocation_receipts(
        authorities=authorities,
        authority_ids=[issue.authority_id],
        runtime_topology=current["runtime_topology"],
        prepared=current,
        plan=current_plan,
        root=tmp_path,
    )
    assert invoked == []
    assert reused == [reference]
    assert load_invocation_receipt(reference, root=tmp_path)[
        "origin_commit_sha"
    ] == HEAD

def test_verifier_consumes_only_lifecycle_selected_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    issue = next(
        item for item in authorities if item.authority_id == "issue-020"
    )
    reference = _write_receipt(
        root=tmp_path,
        prepared=prepared,
        plan=plan,
        authority=issue,
        authorities=authorities,
        runtimes=runtimes,
    )
    prepared["validation_authority_ids"] = [issue.authority_id]
    prepared["invocation_authority_ids"] = []
    prepared["reused_invocations"] = [reference]
    prepared["invocation_shard_assignments"] = []
    monkeypatch.setattr(
        "agent_insights_quality.validation_invocations.validation_runtime_root",
        lambda: tmp_path,
    )
    context = {
        "prepared": prepared,
        "plan": plan,
        "authorities": authorities,
        "paired_baselines": {
            item.canonical_agent: item.authority_id
            for item in authorities
            if item.authority_kind == "baseline"
        },
    }
    references, receipts = _invocation_receipts_for_verification(
        context,
        [issue.authority_id],
    )
    assert references == [reference]
    assert [item["authority_id"] for item in receipts] == [
        issue.authority_id
    ]


@pytest.mark.parametrize(
    "mismatch",
    [
        "source_content",
        "provider_content",
        "provider_version",
        "paired_provider_version",
        "execution",
        "paired_source",
        "paired_execution",
        "runtime_mapping",
        "environment",
        "location",
        "telemetry",
    ],
)
def test_reuse_rejects_every_stale_contract_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    issue = next(
        item for item in authorities if item.authority_id == "issue-020"
    )
    _write_receipt(
        root=tmp_path,
        prepared=prepared,
        plan=plan,
        authority=issue,
        authorities=authorities,
        runtimes=runtimes,
    )
    current_authorities = list(authorities)
    current = copy.deepcopy(prepared)
    current_plan = copy.deepcopy(plan)
    runtime_by_id = {
        item["authority_id"]: item
        for item in current["runtime_topology"]["agents"]
    }
    if mismatch == "source_content":
        current_authorities[current_authorities.index(issue)] = replace(
            issue,
            source_content_digest=content_hash({"changed": "source"}),
        )
    elif mismatch == "provider_content":
        runtime_by_id[issue.authority_id]["provider_content_digest"] = (
            content_hash({"changed": "content"})
        )
    elif mismatch == "provider_version":
        runtime_by_id[issue.authority_id]["provider_agent_version_id"] = (
            "changed-version"
        )
    elif mismatch == "paired_provider_version":
        runtime_by_id[f"{issue.canonical_agent}/v0"][
            "provider_agent_version_id"
        ] = "changed-paired-version"
    elif mismatch == "execution":
        current_authorities[current_authorities.index(issue)] = replace(
            issue,
            execution_digest=content_hash({"changed": "execution"}),
        )
    elif mismatch == "paired_source":
        paired_id = f"{issue.canonical_agent}/v0"
        paired = next(
            item for item in current_authorities if item.authority_id == paired_id
        )
        current_authorities[current_authorities.index(paired)] = replace(
            paired,
            source_content_digest=content_hash({"changed": "paired-source"}),
        )
    elif mismatch == "paired_execution":
        paired_id = f"{issue.canonical_agent}/v0"
        paired = next(
            item for item in current_authorities if item.authority_id == paired_id
        )
        current_authorities[current_authorities.index(paired)] = replace(
            paired,
            execution_digest=content_hash(
                {"changed": "paired-execution"}
            ),
        )
    elif mismatch == "runtime_mapping":
        runtime_by_id[issue.authority_id]["connection_ids"] = [
            "changed-connection"
        ]
    elif mismatch == "environment":
        current["project"]["provider_id"] = "changed-project"
    elif mismatch == "location":
        current_plan["location"] = "changed-location"
    elif mismatch == "telemetry":
        current["substrate"]["telemetry_resource_id"] = "/changed/telemetry"

    selected, reused = select_reusable_invocation_receipts(
        authorities=current_authorities,
        authority_ids=[issue.authority_id],
        runtime_topology=current["runtime_topology"],
        prepared=current,
        plan=current_plan,
        root=tmp_path,
    )
    assert selected == [issue.authority_id]
    assert reused == []


def test_current_same_schema_extractor_imports_40_and_retries_issue_020(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    authority_ids = [item.authority_id for item in authorities]
    assignments = [
        {
            "shard_id": index + 1,
            "authority_ids": authority_ids[index::10],
        }
        for index in range(10)
    ]
    desired = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-desired-state",
        "run_id": RUN_ID,
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "authorities": [
            {
                "authority_id": authority.authority_id,
                "source_content_digest": authority.source_content_digest,
                "provider_content_digest": runtimes[
                    authority.authority_id
                ].provider_content_digest,
            }
            for authority in authorities
        ],
        "desired_state_digest": "",
    }
    desired["desired_state_digest"] = _digest_without(
        desired,
        "desired_state_digest",
    )
    desired_path = tmp_path / "desired-state" / "legacy.json"
    atomic_json(desired_path, desired)
    active = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-lifecycle",
        "state": "VALIDATING",
        **prepared,
        "validation_authority_ids": authority_ids,
        "failure": None,
        "deployment": {"failures": []},
        "shard_assignments": assignments,
        "desired_state_reference": {
            "path": desired_path.relative_to(tmp_path).as_posix(),
            "digest": desired["desired_state_digest"],
        },
        "journal_digest": "",
    }
    active["journal_digest"] = _digest_without(active, "journal_digest")
    active_path = tmp_path / "lifecycle" / "active.json"
    atomic_json(active_path, active)

    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    for assignment in assignments:
        invocations = [
            _invocation(
                next(
                    item
                    for item in authorities
                    if item.authority_id == authority_id
                ),
                qualifier=authority_id,
            )
            for authority_id in assignment["authority_ids"]
        ]
        invocations.sort(key=lambda item: item["authority_id"])
        if "issue-020" in assignment["authority_ids"]:
            ambiguous = next(
                item
                for item in invocations
                if item["authority_id"] == "issue-020"
            )
            attempts = ambiguous["scenarios"][0]["issue_invocations"]
            attempts[0]["response_ids"][0] = attempts[1]["response_ids"][0]
        artifact = {
            "schema_version": "2.0.0",
            "kind": "test-agent-validation-shard-invocations",
            "shard_id": assignment["shard_id"],
            "authority_ids": assignment["authority_ids"],
            "binding": _legacy_binding(
                active,
                assignment["authority_ids"],
                runtime_by_id,
            ),
            "status": "invoked",
            "resources": [
                resource
                for invocation in invocations
                for resource in _resources(
                    invocation,
                    next(
                        item
                        for item in authorities
                        if item.authority_id == invocation["authority_id"]
                    ),
                    runtimes,
                )
                if not (
                    next(
                        item
                        for item in authorities
                        if item.authority_id == invocation["authority_id"]
                    ).runtime_kind
                    != "prompt"
                    and resource["kind"] == "stored_response"
                )
            ],
            "invocations": invocations,
            "artifact_digest": "",
        }
        artifact["artifact_digest"] = _digest_without(
            artifact,
            "artifact_digest",
        )
        artifact_path = (
            tmp_path
            / "shards"
            / "ninghu"
            / "agent-insights-quality"
            / "999"
            / RUN_ID
            / f"shard-{assignment['shard_id']:02d}"
            / "invocations.json"
        )
        atomic_json(artifact_path, artifact)

    with extract_legacy_shard_invocations(
        active_path=active_path,
        plan=plan,
        authorities=authorities,
        root=tmp_path,
    ) as migration:
        assert migration["incomplete_authority_ids"] == ["issue-020"]
        assert len(migration["imported_authority_ids"]) == 40
        source_lock = LocalValidationLock(
            tmp_path
            / "shards"
            / "ninghu"
            / "agent-insights-quality"
            / "999"
            / RUN_ID
            / "shard-01"
            / "validation.lock"
        )
        with pytest.raises(ContractError, match="holds the shared lock"):
            source_lock.acquire()
    with source_lock:
        assert source_lock.owned

    invoke_ids, reused = select_reusable_invocation_receipts(
        authorities=authorities,
        authority_ids=authority_ids,
        runtime_topology=prepared["runtime_topology"],
        prepared=prepared,
        plan=plan,
        forced_authority_ids=set(migration["incomplete_authority_ids"]),
        root=tmp_path,
    )
    assert invoke_ids == ["issue-020"]
    assert len(reused) == 40
    hosted_reference = next(
        reference
        for reference in reused
        if next(
            item
            for item in authorities
            if item.authority_id == reference["authority_id"]
        ).runtime_kind
        != "prompt"
    )
    hosted_receipt = load_invocation_receipt(
        hosted_reference,
        root=tmp_path,
    )
    assert hosted_receipt["migrated_from"]["schema_version"] == "2.0.0"
    assert {
        item["kind"] for item in hosted_receipt["resources"]
    } == {"session"}


@pytest.mark.parametrize("shard_id", [1, 4, 5])
def test_legacy_artifact_accepts_exact_lexical_authority_order(
    monkeypatch,
    tmp_path: Path,
    shard_id: int,
) -> None:
    prepared, _, authorities, _ = _context()
    authority_ids = [item.authority_id for item in authorities]
    assignment = {
        "shard_id": shard_id,
        "authority_ids": authority_ids[shard_id - 1 :: 10],
    }
    assert assignment["authority_ids"] != sorted(assignment["authority_ids"])
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    active = {
        **prepared,
        "digests": prepared["digests"],
    }
    artifact = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-shard-invocations",
        "shard_id": shard_id,
        "authority_ids": assignment["authority_ids"],
        "binding": _legacy_binding(
            active,
            assignment["authority_ids"],
            runtime_by_id,
        ),
        "status": "invoked",
        "resources": [],
        "invocations": [
            {"authority_id": item}
            for item in sorted(assignment["authority_ids"])
        ],
        "artifact_digest": "",
    }
    artifact["artifact_digest"] = _digest_without(
        artifact,
        "artifact_digest",
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_invocations.read_json",
        lambda _path: copy.deepcopy(artifact),
    )
    assert _read_legacy_shard_artifact(
        active=active,
        assignment=assignment,
        root=tmp_path,
    ) == artifact


@pytest.mark.parametrize(
    "sequence_kind",
    ["duplicate", "missing", "extra", "unsorted"],
)
def test_legacy_artifact_rejects_unexpected_authority_sequences(
    monkeypatch,
    tmp_path: Path,
    sequence_kind: str,
) -> None:
    prepared, _, authorities, _ = _context()
    authority_ids = [item.authority_id for item in authorities]
    assignment = {
        "shard_id": 1,
        "authority_ids": authority_ids[::10],
    }
    actual = sorted(assignment["authority_ids"])
    if sequence_kind == "duplicate":
        actual[-1] = actual[0]
    elif sequence_kind == "missing":
        actual.pop()
    elif sequence_kind == "extra":
        actual.append("issue-999")
    else:
        actual = list(reversed(actual))
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    active = {**prepared, "digests": prepared["digests"]}
    artifact = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-shard-invocations",
        "shard_id": 1,
        "authority_ids": assignment["authority_ids"],
        "binding": _legacy_binding(
            active,
            assignment["authority_ids"],
            runtime_by_id,
        ),
        "status": "invoked",
        "resources": [],
        "invocations": [{"authority_id": item} for item in actual],
        "artifact_digest": "",
    }
    artifact["artifact_digest"] = _digest_without(
        artifact,
        "artifact_digest",
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_invocations.read_json",
        lambda _path: copy.deepcopy(artifact),
    )
    assert (
        _read_legacy_shard_artifact(
            active=active,
            assignment=assignment,
            root=tmp_path,
        )
        is None
    )


def test_supplemental_migration_recovers_only_original_marker_misses(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    temporary_archive, source, assignments = _write_complete_legacy_generation(
        tmp_path,
        prepared=prepared,
        authorities=authorities,
        runtimes=runtimes,
    )
    missed = [
        authority_id
        for assignment in assignments
        if assignment["shard_id"] in {1, 4, 5}
        for authority_id in assignment["authority_ids"]
    ]
    imported = [
        item.authority_id
        for item in authorities
        if item.authority_id not in set(missed)
    ]
    authority_by_id = {item.authority_id: item for item in authorities}
    stale_plan = copy.deepcopy(plan)
    stale_plan["invocation_contract_digest"] = content_hash(
        {"legacy_contract": 1}
    )
    for authority_id in imported:
        authority = authority_by_id[authority_id]
        assignment = next(
            item
            for item in assignments
            if authority_id in item["authority_ids"]
        )
        artifact_path = (
            tmp_path
            / "shards"
            / "ninghu"
            / "agent-insights-quality"
            / "999"
            / RUN_ID
            / f"shard-{assignment['shard_id']:02d}"
            / "invocations.json"
        )
        artifact = read_json(artifact_path)
        invocation = next(
            item
            for item in artifact["invocations"]
            if item["authority_id"] == authority_id
        )
        paired = authority_by_id[f"{authority.canonical_agent}/v0"]
        write_invocation_receipt(
            prepared=source,
            plan=stale_plan,
            shard_id=assignment["shard_id"],
            authority=authority,
            runtime=runtimes[authority_id],
            paired_v0_authority=(
                None
                if authority.authority_kind == "baseline"
                else paired
            ),
            paired_v0_runtime=(
                None
                if authority.authority_kind == "baseline"
                else runtimes[paired.authority_id]
            ),
            invocation=invocation,
            resources=_resources_for_invocation(
                artifact["resources"],
                invocation,
            ),
            fence=lambda: None,
            root=tmp_path,
            migrated_from={
                "schema_version": "2.0.0",
                "kind": "test-agent-validation-shard-invocations",
                "artifact_digest": artifact["artifact_digest"],
            },
        )
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": source["journal_digest"],
        "imported_authority_ids": imported,
        "incomplete_authority_ids": missed,
        "migration_digest": "",
    }
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    marker_path = (
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json"
    )
    atomic_json(marker_path, marker)
    original_marker_bytes = marker_path.read_bytes()

    archive_digest = file_hash(temporary_archive)
    archive_path = (
        tmp_path
        / "lifecycle"
        / "superseded-formats"
        / f"{archive_digest.removeprefix('sha256:')}.json"
    )
    archive_path.parent.mkdir(parents=True)
    temporary_archive.replace(archive_path)
    current_active = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-lifecycle",
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "invocation_authority_ids": missed,
        "supersedes": content_hash({"immediate_generation": 2}),
        "journal_digest": "",
    }
    current_active["journal_digest"] = _digest_without(
        current_active,
        "journal_digest",
    )
    active_path = tmp_path / "lifecycle" / "active.json"
    atomic_json(active_path, current_active)

    with recover_supplemental_legacy_invocations(
        active_path=active_path,
        plan=plan,
        authorities=authorities,
        root=tmp_path,
    ) as result:
        assert {
            item["authority_id"]
            for item in prepared["runtime_topology"]["agents"]
        } == set(result["imported_authority_ids"])
        assert result["incomplete_authority_ids"] == []
    assert marker_path.read_bytes() == original_marker_bytes
    supplemental_path = (
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1-supplemental.json"
    )
    supplemental = read_json(supplemental_path)
    assert supplemental["source_marker_digest"] == marker["migration_digest"]
    assert supplemental["source_lifecycle_digest"] == source["journal_digest"]
    assert supplemental["source_archive_digest"] == archive_digest
    selected, reused = select_reusable_invocation_receipts(
        authorities=authorities,
        authority_ids=[item.authority_id for item in authorities],
        runtime_topology=prepared["runtime_topology"],
        prepared=prepared,
        plan=plan,
        root=tmp_path,
    )
    assert selected == []
    assert len(reused) == 41
    current_active["schema_version"] = "3.0.0"
    current_active["journal_digest"] = _digest_without(
        current_active,
        "journal_digest",
    )
    atomic_json(active_path, current_active)
    archive_path.unlink()
    with recover_supplemental_legacy_invocations(
        active_path=active_path,
        plan=plan,
        authorities=authorities,
        root=tmp_path,
    ) as repeated:
        assert repeated == result


def test_completed_supplemental_migration_replays_without_archive(
    tmp_path: Path,
) -> None:
    _, plan, authorities, _, marker, supplemental = (
        _write_completed_supplemental_replay_state(tmp_path)
    )

    with recover_supplemental_legacy_invocations(
        active_path=tmp_path / "lifecycle" / "active.json",
        plan=plan,
        authorities=authorities,
        root=tmp_path,
    ) as result:
        assert len(marker["imported_authority_ids"]) == 28
        assert len(marker["incomplete_authority_ids"]) == 13
        assert len(supplemental["imported_authority_ids"]) == 13
        assert supplemental["incomplete_authority_ids"] == []
        assert result == {
            "source_run_id": RUN_ID,
            "imported_authority_ids": [
                item.authority_id for item in authorities
            ],
            "incomplete_authority_ids": [],
        }
    assert not list((tmp_path / "lifecycle" / "superseded-formats").glob("*.json"))


@pytest.mark.parametrize("fault", ["intrinsic-digest", "source-binding"])
def test_completed_supplemental_migration_rejects_invalid_marker(
    tmp_path: Path,
    fault: str,
) -> None:
    _, plan, authorities, _, _, supplemental = (
        _write_completed_supplemental_replay_state(tmp_path)
    )
    if fault == "intrinsic-digest":
        supplemental["source_archive_digest"] = content_hash(
            {"archive": "corrupted"}
        )
    else:
        supplemental["source_marker_digest"] = content_hash(
            {"marker": "other-source"}
        )
        supplemental["migration_digest"] = _digest_without(
            supplemental,
            "migration_digest",
        )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1-supplemental.json",
        supplemental,
    )

    with pytest.raises(
        ContractError,
        match="Supplemental invocation migration marker is invalid",
    ):
        with recover_supplemental_legacy_invocations(
            active_path=tmp_path / "lifecycle" / "active.json",
            plan=plan,
            authorities=authorities,
            root=tmp_path,
        ):
            pass


@pytest.mark.parametrize("fault", ["malformed", "mixed-source", "cross-pr"])
def test_completed_supplemental_migration_rejects_invalid_receipt_set(
    tmp_path: Path,
    fault: str,
) -> None:
    _, plan, authorities, receipt_paths, _, _ = (
        _write_completed_supplemental_replay_state(
            tmp_path,
            mixed_receipt=fault == "mixed-source",
            cross_pr_receipt=fault == "cross-pr",
        )
    )
    if fault == "malformed":
        receipt = read_json(receipt_paths[0])
        receipt["receipt_digest"] = content_hash({"receipt": "malformed"})
        atomic_json(receipt_paths[0], receipt)
    with pytest.raises(
        ContractError,
        match="Completed invocation migration receipt binding changed",
    ):
        with recover_supplemental_legacy_invocations(
            active_path=tmp_path / "lifecycle" / "active.json",
            plan=plan,
            authorities=authorities,
            root=tmp_path,
        ):
            pass


@pytest.mark.parametrize("archive", ["missing", "mismatched", "matching"])
def test_incomplete_supplemental_migration_requires_exact_archive(
    tmp_path: Path,
    archive: str,
) -> None:
    prepared, plan, authorities, _, marker, supplemental = (
        _write_completed_supplemental_replay_state(tmp_path)
    )
    supplemental["imported_authority_ids"] = marker[
        "incomplete_authority_ids"
    ][:-1]
    supplemental["incomplete_authority_ids"] = marker[
        "incomplete_authority_ids"
    ][-1:]
    archive_digest = None
    if archive != "missing":
        source = {
            "schema_version": "2.0.0",
            "kind": "test-agent-validation-lifecycle",
            "state": "VALIDATING",
            "run_id": marker["source_run_id"],
            "journal_digest": marker["source_lifecycle_digest"],
            "repository": plan["repository"],
            "pr_number": plan["pr_number"],
            "validation_authority_ids": [
                item.authority_id for item in authorities
            ],
        }
        temporary = tmp_path / "source-active.json"
        atomic_json(temporary, source)
        archive_digest = file_hash(temporary)
        archive_path = (
            tmp_path
            / "lifecycle"
            / "superseded-formats"
            / f"{archive_digest.removeprefix('sha256:')}.json"
        )
        archive_path.parent.mkdir(parents=True)
        temporary.replace(archive_path)
    if archive == "matching":
        supplemental["source_archive_digest"] = archive_digest
    supplemental["migration_digest"] = _digest_without(
        supplemental,
        "migration_digest",
    )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1-supplemental.json",
        supplemental,
    )

    if archive == "missing":
        error = "exactly one source archive"
    elif archive == "mismatched":
        error = "Supplemental invocation migration marker is invalid"
    else:
        error = None
    if error is not None:
        with pytest.raises(ContractError, match=error):
            with recover_supplemental_legacy_invocations(
                active_path=tmp_path / "lifecycle" / "active.json",
                plan=plan,
                authorities=authorities,
                root=tmp_path,
            ):
                pass
        return
    with recover_supplemental_legacy_invocations(
        active_path=tmp_path / "lifecycle" / "active.json",
        plan=plan,
        authorities=authorities,
        root=tmp_path,
    ) as result:
        assert len(result["imported_authority_ids"]) == 40
        assert len(result["incomplete_authority_ids"]) == 1
        assert result["source_run_id"] == prepared["run_id"]


def test_completed_original_migration_replays_without_legacy_active(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    authority_ids = [item.authority_id for item in authorities]
    assert len(authority_ids) == 41
    for authority in authorities:
        _write_receipt(
            root=tmp_path,
            prepared=prepared,
            plan=plan,
            authority=authority,
            authorities=authorities,
            runtimes=runtimes,
            qualifier=f"complete-{authority.authority_id}",
            migrated_from={
                "schema_version": "2.0.0",
                "kind": "test-agent-validation-shard-invocations",
                "artifact_digest": content_hash(
                    {"authority_id": authority.authority_id}
                ),
            },
        )
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": prepared["journal_digest"],
        "imported_authority_ids": authority_ids,
        "incomplete_authority_ids": [],
        "migration_digest": "",
    }
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json",
        marker,
    )
    active_path = tmp_path / "lifecycle" / "active.json"

    with recover_supplemental_legacy_invocations(
        active_path=active_path,
        plan=plan,
        authorities=authorities,
        root=tmp_path,
    ) as result:
        assert result == {
            "source_run_id": RUN_ID,
            "imported_authority_ids": authority_ids,
            "incomplete_authority_ids": [],
        }


def test_completed_migration_accepts_coherent_historical_receipt_pr(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    authority_ids = [item.authority_id for item in authorities]
    for authority in authorities:
        _write_receipt(
            root=tmp_path,
            prepared=prepared,
            plan=plan,
            authority=authority,
            authorities=authorities,
            runtimes=runtimes,
            qualifier=f"cross-pr-{authority.authority_id}",
            migrated_from={
                "schema_version": "2.0.0",
                "kind": "test-agent-validation-shard-invocations",
                "artifact_digest": content_hash(
                    {"authority_id": authority.authority_id}
                ),
            },
        )
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": prepared["journal_digest"],
        "imported_authority_ids": authority_ids,
        "incomplete_authority_ids": [],
        "migration_digest": "",
    }
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json",
        marker,
    )
    cross_pr_plan = copy.deepcopy(plan)
    cross_pr_plan["pr_number"] += 1

    with recover_supplemental_legacy_invocations(
        active_path=tmp_path / "lifecycle" / "active.json",
        plan=cross_pr_plan,
        authorities=authorities,
        root=tmp_path,
    ) as result:
        assert result["imported_authority_ids"] == authority_ids
        assert result["incomplete_authority_ids"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_run_id", ""),
        ("source_lifecycle_digest", "sha256:not-a-digest"),
    ],
)
def test_completed_migration_rejects_malformed_source_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _, plan, authorities, _ = _context()
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": content_hash({"legacy": "source"}),
        "imported_authority_ids": [
            item.authority_id for item in authorities
        ],
        "incomplete_authority_ids": [],
        "migration_digest": "",
    }
    marker[field] = value
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json",
        marker,
    )

    with pytest.raises(ContractError, match="migration marker is invalid"):
        with recover_supplemental_legacy_invocations(
            active_path=tmp_path / "lifecycle" / "active.json",
            plan=plan,
            authorities=authorities,
            root=tmp_path,
        ):
            pass


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "cross-authority"])
def test_completed_migration_requires_exact_current_authority_coverage(
    tmp_path: Path,
    mutation: str,
) -> None:
    _, plan, authorities, _ = _context()
    authority_ids = [item.authority_id for item in authorities]
    if mutation == "missing":
        imported = authority_ids[:-1]
    elif mutation == "duplicate":
        imported = [*authority_ids[:-1], authority_ids[0]]
    else:
        imported = [*authority_ids[:-1], "other-agent/issue-999"]
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": content_hash({"legacy": "source"}),
        "imported_authority_ids": imported,
        "incomplete_authority_ids": [],
        "migration_digest": "",
    }
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json",
        marker,
    )

    with pytest.raises(
        ContractError,
        match="Supplemental invocation migration authority coverage changed",
    ):
        with recover_supplemental_legacy_invocations(
            active_path=tmp_path / "lifecycle" / "active.json",
            plan=plan,
            authorities=authorities,
            root=tmp_path,
        ):
            pass


def test_incomplete_migration_still_requires_exact_source_archive(
    tmp_path: Path,
) -> None:
    _, plan, authorities, _ = _context()
    authority_ids = [item.authority_id for item in authorities]
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": content_hash({"legacy": "source"}),
        "imported_authority_ids": authority_ids[:-1],
        "incomplete_authority_ids": authority_ids[-1:],
        "migration_digest": "",
    }
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json",
        marker,
    )

    with pytest.raises(ContractError, match="exactly one source archive"):
        with recover_supplemental_legacy_invocations(
            active_path=tmp_path / "lifecycle" / "active.json",
            plan=plan,
            authorities=authorities,
            root=tmp_path,
        ):
            pass


def test_first_supplemental_extraction_rejects_non_v2_active(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    temporary_archive, source, _ = _write_complete_legacy_generation(
        tmp_path,
        prepared=prepared,
        authorities=authorities,
        runtimes=runtimes,
    )
    archive_digest = file_hash(temporary_archive)
    archive_path = (
        tmp_path
        / "lifecycle"
        / "superseded-formats"
        / f"{archive_digest.removeprefix('sha256:')}.json"
    )
    archive_path.parent.mkdir(parents=True)
    temporary_archive.replace(archive_path)
    authority_ids = [item.authority_id for item in authorities]
    marker = {
        "schema_version": "1.0.0",
        "kind": "shard-invocations-v2-to-authority-receipts-v1",
        "source_run_id": RUN_ID,
        "source_lifecycle_digest": source["journal_digest"],
        "imported_authority_ids": authority_ids[:-1],
        "incomplete_authority_ids": authority_ids[-1:],
        "migration_digest": "",
    }
    marker["migration_digest"] = _digest_without(
        marker,
        "migration_digest",
    )
    atomic_json(
        tmp_path
        / "migrations"
        / "shard-invocations-v2-to-authority-receipts-v1.json",
        marker,
    )
    active_path = tmp_path / "lifecycle" / "active.json"
    current_active = {
        "schema_version": "3.0.0",
        "kind": "test-agent-validation-lifecycle",
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "journal_digest": "",
    }
    current_active["journal_digest"] = _digest_without(
        current_active,
        "journal_digest",
    )
    atomic_json(active_path, current_active)

    with pytest.raises(
        ContractError,
        match="Supplemental invocation migration active binding is invalid",
    ):
        with recover_supplemental_legacy_invocations(
            active_path=active_path,
            plan=plan,
            authorities=authorities,
            root=tmp_path,
        ):
            pass


@pytest.mark.parametrize(
    ("imported", "incomplete"),
    [
        (["issue-001"], ["issue-001", "issue-002"]),
        (["issue-001"], []),
    ],
)
def test_migration_authority_union_rejects_overlap_or_gap(
    imported: list[str],
    incomplete: list[str],
) -> None:
    with pytest.raises(ContractError, match="coverage mismatch"):
        _validate_migration_authority_coverage(
            source_ids=["issue-001", "issue-002"],
            imported=imported,
            incomplete=incomplete,
            message="coverage mismatch",
        )


def test_archive_locator_ignores_intervening_new_schema_generations(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    temporary, source, _ = _write_complete_legacy_generation(
        tmp_path,
        prepared=prepared,
        authorities=authorities,
        runtimes=runtimes,
    )
    archive_root = tmp_path / "lifecycle" / "superseded-formats"
    archive_root.mkdir(parents=True)
    digest = file_hash(temporary)
    expected = archive_root / f"{digest.removeprefix('sha256:')}.json"
    temporary.replace(expected)
    for generation in (2, 3):
        intervening = {
            "schema_version": "2.0.0",
            "kind": "test-agent-validation-lifecycle",
            "run_id": f"validation-{generation:012x}",
            "invocation_authority_ids": ["issue-020"],
        }
        path = archive_root / f"intervening-{generation}.json"
        atomic_json(path, intervening)
        valid = archive_root / (
            f"{file_hash(path).removeprefix('sha256:')}.json"
        )
        path.replace(valid)
    marker = {
        "source_run_id": source["run_id"],
        "source_lifecycle_digest": source["journal_digest"],
    }
    path, observed_digest, observed = _locate_legacy_source_archive(
        root=tmp_path,
        source_marker=marker,
        plan=plan,
    )
    assert path == expected
    assert observed_digest == digest
    assert observed == source


def test_archive_locator_rejects_no_match_duplicate_and_corrupt_hash(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    temporary, source, _ = _write_complete_legacy_generation(
        tmp_path,
        prepared=prepared,
        authorities=authorities,
        runtimes=runtimes,
    )
    archive_root = tmp_path / "lifecycle" / "superseded-formats"
    archive_root.mkdir(parents=True)
    marker = {
        "source_run_id": source["run_id"],
        "source_lifecycle_digest": source["journal_digest"],
    }
    with pytest.raises(ContractError, match="exactly one source archive"):
        _locate_legacy_source_archive(
            root=tmp_path,
            source_marker=marker,
            plan=plan,
        )

    digest = file_hash(temporary)
    first = archive_root / f"{digest.removeprefix('sha256:')}.json"
    temporary.replace(first)
    alternate = archive_root / "alternate.json"
    alternate.write_text(
        json.dumps(source, separators=(",", ":")),
        encoding="utf-8",
    )
    alternate_digest = file_hash(alternate)
    alternate.rename(
        archive_root
        / f"{alternate_digest.removeprefix('sha256:')}.json"
    )
    with pytest.raises(ContractError, match="exactly one source archive"):
        _locate_legacy_source_archive(
            root=tmp_path,
            source_marker=marker,
            plan=plan,
        )

    first.unlink()
    for path in archive_root.glob("*.json"):
        path.unlink()
    corrupt = archive_root / f"{'0' * 64}.json"
    corrupt.write_text("{}", encoding="utf-8")
    with pytest.raises(ContractError, match="filename digest changed"):
        _locate_legacy_source_archive(
            root=tmp_path,
            source_marker=marker,
            plan=plan,
        )


def test_corrupt_invocation_receipt_is_never_reused(tmp_path: Path) -> None:
    prepared, plan, authorities, runtimes = _context()
    issue = next(
        item for item in authorities if item.authority_id == "issue-020"
    )
    reference = _write_receipt(
        root=tmp_path,
        prepared=prepared,
        plan=plan,
        authority=issue,
        authorities=authorities,
        runtimes=runtimes,
    )
    path = tmp_path / reference["path"]
    receipt = read_json(path)
    receipt["invocation"]["scenarios"][0]["issue_invocations"][0][
        "completed_at"
    ] = "2026-08-31T13:00:00+00:00"
    atomic_json(path, receipt)

    selected, reused = select_reusable_invocation_receipts(
        authorities=authorities,
        authority_ids=[issue.authority_id],
        runtime_topology=prepared["runtime_topology"],
        prepared=prepared,
        plan=plan,
        root=tmp_path,
    )
    assert selected == [issue.authority_id]
    assert reused == []


def test_receipt_rejects_empty_resource_provenance(tmp_path: Path) -> None:
    prepared, plan, authorities, runtimes = _context()
    issue = next(
        item for item in authorities if item.authority_id == "issue-020"
    )
    paired = next(
        item
        for item in authorities
        if item.authority_id == f"{issue.canonical_agent}/v0"
    )
    with pytest.raises(ContractError, match="resources|resource provenance"):
        write_invocation_receipt(
            prepared=prepared,
            plan=plan,
            shard_id=1,
            authority=issue,
            runtime=runtimes[issue.authority_id],
            paired_v0_authority=paired,
            paired_v0_runtime=runtimes[paired.authority_id],
            invocation=_invocation(issue),
            resources=[],
            fence=lambda: None,
            root=tmp_path,
        )


def test_current_hosted_receipt_requires_response_resource_provenance(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    issue = next(
        item
        for item in authorities
        if item.runtime_kind != "prompt" and item.authority_kind == "issue"
    )
    paired = next(
        item
        for item in authorities
        if item.authority_id == f"{issue.canonical_agent}/v0"
    )
    invocation = _invocation(issue)
    legacy_resources = [
        item
        for item in _resources(invocation, issue, runtimes)
        if item["kind"] == "session"
    ]
    with pytest.raises(ContractError, match="coverage is incomplete"):
        write_invocation_receipt(
            prepared=prepared,
            plan=plan,
            shard_id=1,
            authority=issue,
            runtime=runtimes[issue.authority_id],
            paired_v0_authority=paired,
            paired_v0_runtime=runtimes[paired.authority_id],
            invocation=invocation,
            resources=legacy_resources,
            fence=lambda: None,
            root=tmp_path,
        )


def test_receipt_set_rejects_cross_authority_hosted_session_reuse(
    tmp_path: Path,
) -> None:
    prepared, plan, authorities, runtimes = _context()
    hosted = [
        item
        for item in authorities
        if item.runtime_kind != "prompt" and item.authority_kind == "issue"
    ][:2]
    references = [
        _write_receipt(
            root=tmp_path,
            prepared=prepared,
            plan=plan,
            authority=authority,
            authorities=authorities,
            runtimes=runtimes,
            qualifier=authority.authority_id,
        )
        for authority in hosted
    ]
    receipts = [
        load_invocation_receipt(item, root=tmp_path)
        for item in references
    ]
    first_session = receipts[0]["invocation"]["scenarios"][0][
        "issue_invocations"
    ][0]["session_id"]
    receipts[1]["invocation"]["scenarios"][0]["issue_invocations"][0][
        "session_id"
    ] = first_session
    with pytest.raises(ContractError, match="references collide"):
        assert_invocation_receipt_set_isolated(receipts)


def _legacy_binding(
    active: dict,
    authority_ids: list[str],
    runtime_by_id: dict[str, dict],
) -> dict:
    required = set(authority_ids)
    required.update(
        f"{runtime_by_id[item]['canonical_agent']}/v0"
        for item in authority_ids
    )
    return {
        "repository": active["repository"],
        "pr_number": active["pr_number"],
        "commit_sha": active["commit_sha"],
        "run_id": active["run_id"],
        "validation_digest": active["digests"]["validation_digest"],
        "execution_matrix_digest": active["digests"][
            "execution_matrix_digest"
        ],
        "runtime_topology_digest": active["digests"][
            "runtime_topology_digest"
        ],
        "project_id": active["project"]["provider_id"],
        "authorities": [
            {
                field: runtime_by_id[authority_id][field]
                for field in (
                    "authority_id",
                    "runtime_agent_name",
                    "runtime_agent_version",
                    "provider_agent_id",
                    "provider_agent_version_id",
                    "provider_content_digest",
                )
            }
            for authority_id in sorted(required)
        ],
    }


def _digest_without(value: dict, field: str) -> str:
    payload = copy.deepcopy(value)
    payload.pop(field, None)
    return content_hash(payload)
