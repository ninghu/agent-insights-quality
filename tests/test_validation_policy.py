from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_policy import (
    VALIDATION_CONFIG_PATH,
    load_validation_policy,
)


def test_validation_policy_freezes_local_inventory_and_limits() -> None:
    policy = load_validation_policy()
    assert policy.environment_id == "swedencentral-g30"
    assert policy.location == "swedencentral"
    assert policy.project_name == "aiq-staging-swedencentral"
    assert policy.telemetry_resource_set == "g30"
    assert policy.limits.provisioning_concurrency == 8
    assert policy.limits.telemetry_query_concurrency == 8
    assert policy.limits.runtime_attempt_concurrency == 1
    assert policy.limits.absolute_ttl_hours == 72
    assert policy.limits.active_heartbeat_seconds == 60
    assert policy.trace_hydration_poll_seconds == 15
    assert policy.trace_hydration_stabilization_seconds == 180
    assert policy.trace_hydration_maximum_wait_seconds == 900
    assert policy.verification_maximum_active_subsessions == 8
    assert policy.verification_authorities_per_assignment == 1
    assert policy.verification_response_bound_batch_scope == "target"


def test_validation_policy_rejects_threshold_or_telemetry_drift(tmp_path) -> None:
    value = yaml.safe_load(VALIDATION_CONFIG_PATH.read_text(encoding="utf-8"))
    changed = deepcopy(value)
    changed["limits"]["provisioning_concurrency"] = 9
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    with pytest.raises(ContractError, match="limits differ"):
        load_validation_policy(path)

    changed = deepcopy(value)
    changed["telemetry_resource_set"] = "g29"
    path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    with pytest.raises(ContractError, match="reviewed Sweden"):
        load_validation_policy(path)


def test_validation_has_no_separate_trust_policy_manifest() -> None:
    assert not (
        VALIDATION_CONFIG_PATH.parent / "test-agent-validation-policy.yaml"
    ).exists()


def test_validation_policy_rejects_removed_provenance_keys(tmp_path) -> None:
    value = yaml.safe_load(VALIDATION_CONFIG_PATH.read_text(encoding="utf-8"))
    value["default_branch"] = "main"
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ContractError, match="config fields"):
        load_validation_policy(path)
