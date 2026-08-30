from __future__ import annotations

from datetime import UTC, datetime

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.validation_cycle import initial_lifecycle
from agent_insights_quality.validation_lifecycle import validate_lifecycle
from agent_insights_quality.validation_manifest import (
    prepare_candidate_manifest,
    stamp_candidate_manifest,
)
from agent_insights_quality.validation_policy import (
    load_trusted_policy,
    load_validation_policy,
)


def test_initial_cycle_binds_candidate_policy_and_immutable_ttl() -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    trusted, trusted_digest = load_trusted_policy()
    candidate = stamp_candidate_manifest(
        prepare_candidate_manifest(
            agents=agents,
            issues=issues,
            policy=policy,
            repository=policy.repository,
            pr_number=999,
            candidate_head_sha="a" * 40,
            candidate_tree_sha="b" * 40,
            workflow_run_id="synthetic-run",
        )
    )
    value = initial_lifecycle(
        candidate,
        policy=policy,
        policy_manifest=trusted,
        policy_manifest_digest=trusted_digest,
        policy_commit_sha="a" * 40,
        policy_ref="a" * 40,
        lease_id="synthetic-lease",
        ownership_nonce="nonce-0001",
        holder_workflow_reference="workflow",
        holder_app_reference="app",
        holder_run_reference="run",
        account_reference="sha256:" + ("c" * 64),
        now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )
    validate_lifecycle(value)
    assert value["state"] == "LEASED"
    assert value["capacity"] is None
    assert value["absolute_expires_at"] == "2026-09-01T12:00:00+00:00"
