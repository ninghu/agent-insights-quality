from __future__ import annotations

import copy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import atomic_json, content_hash, read_json
from agent_insights_quality.validation_invocations import (
    extract_legacy_shard_invocations,
    load_invocation_receipt,
    select_reusable_invocation_receipts,
    write_invocation_receipt,
)
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_validation_plan,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_runtime import DeployedRuntime
from agent_insights_quality.validation_shards import ValidationShardStore

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
    scenarios = []
    for scenario in authority.validation_rules["scenarios"]:
        scenarios.append(
            {
                "scenario_id": scenario["id"],
                "issue_invocations": [
                    _attempt_invocation(
                        attempt,
                        qualifier=f"{qualifier}-issue-{attempt['index']}",
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


def _attempt_invocation(attempt, *, qualifier: str) -> dict:
    count = len(attempt["setup_steps"]) + len(attempt["probe_steps"])
    started = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    return {
        "started_at": started.isoformat(),
        "completed_at": (started + timedelta(seconds=1)).isoformat(),
        "response_ids": [
            f"response-{qualifier}-{index}" for index in range(1, count + 1)
        ],
        "usable_results": [True] * count,
        "session_id": f"session-{qualifier}",
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
) -> dict[str, str]:
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
        invocation=_invocation(authority, qualifier=qualifier),
        resources=[],
        fence=lambda: None,
        root=root,
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

    store = ValidationShardStore(
        prepared=current,
        shard_id=1,
        authority_ids=[issue.authority_id],
        fence=lambda: None,
    )
    package = store.write_package(
        authorities=[
            {
                "authority_id": issue.authority_id,
                "validated_commit_sha": NEXT_HEAD,
            }
        ],
        invocation_receipts=reused,
    )
    assert package["verifier_commit_sha"] == NEXT_HEAD
    assert package["verifier_digest"] == current["digests"][
        "shared_validation_digest"
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
        "invoker",
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
    elif mismatch == "invoker":
        current_plan["invocation_contract_digest"] = content_hash(
            {"changed": "invoker"}
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
            "authority_ids": authority_ids[index::8],
        }
        for index in range(8)
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
            "resources": [],
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

    migration = extract_legacy_shard_invocations(
        active_path=active_path,
        plan=plan,
        authorities=authorities,
        root=tmp_path,
    )
    assert migration["incomplete_authority_ids"] == ["issue-020"]
    assert len(migration["imported_authority_ids"]) == 40

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
