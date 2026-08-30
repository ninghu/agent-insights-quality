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
    assert policy.authority_count == 41
    assert policy.telemetry_resource_set == "g29"
    assert policy.limits.provisioning_concurrency == 8
    assert policy.limits.telemetry_query_concurrency == 4
    assert policy.limits.runtime_attempt_concurrency == 1
    assert policy.limits.absolute_ttl_hours == 72
    assert policy.limits.active_heartbeat_seconds == 60
    assert policy.documented_project_cascade == ("runtime_principal",)


def test_validation_policy_rejects_threshold_or_telemetry_drift(tmp_path) -> None:
    value = yaml.safe_load(VALIDATION_CONFIG_PATH.read_text(encoding="utf-8"))
    changed = deepcopy(value)
    changed["limits"]["provisioning_concurrency"] = 9
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    with pytest.raises(ContractError, match="limits differ"):
        load_validation_policy(path)

    changed = deepcopy(value)
    changed["telemetry_resource_set"] = "g30"
    path.write_text(yaml.safe_dump(changed), encoding="utf-8")
    with pytest.raises(ContractError, match="read-only g29"):
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
