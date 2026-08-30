from __future__ import annotations

from datetime import UTC, datetime

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import content_hash
from agent_insights_quality.validation_cycle import initial_lifecycle
from agent_insights_quality.validation_lifecycle import validate_lifecycle
from agent_insights_quality.validation_manifest import prepare_validation_plan
from agent_insights_quality.validation_policy import load_validation_policy


def test_initial_cycle_binds_one_commit_operator_and_immutable_ttl() -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    plan = prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        local_run_id="synthetic-run",
    )
    value = initial_lifecycle(
        plan,
        policy=policy,
        ownership_nonce="nonce-0001",
        holder_session_reference=content_hash("session"),
        holder_operator_reference=content_hash("operator"),
        holder_run_reference=content_hash("run"),
        account_reference=content_hash("account"),
        now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )
    validate_lifecycle(value)
    assert value["state"] == "LOCKED"
    assert value["commit_sha"] == "a" * 40
    assert value["capacity"] is None
    assert value["absolute_expires_at"] == "2026-09-01T12:00:00+00:00"
    assert "git" not in value
    assert "policy_manifest" not in value
    assert "lease" not in value
