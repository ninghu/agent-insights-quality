from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_insights_quality.util import ROOT, ContractError, read_yaml

VALIDATION_CONFIG_PATH = ROOT / "config" / "test-agent-validation.yaml"


@dataclass(frozen=True)
class NamePolicy:
    maximum_length: int
    pattern: str

    def accepts(self, value: str) -> bool:
        return len(value) <= self.maximum_length and re.fullmatch(
            self.pattern,
            value,
        ) is not None


@dataclass(frozen=True)
class ValidationLimits:
    provisioning_concurrency: int
    telemetry_query_concurrency: int
    runtime_attempt_concurrency: int
    inner_model_call_limit: int
    reserved_capacity_percent: int
    minimum_rpm_headroom: int
    minimum_tpm_headroom: int
    active_heartbeat_seconds: int
    absolute_ttl_hours: int
    max_recovery_versions_per_agent: int


@dataclass(frozen=True)
class ValidationPolicy:
    repository: str
    environment_id: str
    location: str
    project_name: str
    telemetry_resource_set: str
    test_agent_model: dict[str, str]
    prompt_canary_agent: str
    hosted_canary_agent: str
    authority_count: int
    limits: ValidationLimits
    project_name_policy: NamePolicy
    agent_name_policy: NamePolicy
    resource_kinds: tuple[str, ...]
    documented_project_cascade: tuple[str, ...]
    trace_hydration_poll_seconds: int
    trace_hydration_stabilization_seconds: int
    trace_hydration_maximum_wait_seconds: int


def load_validation_policy(
    path: Path = VALIDATION_CONFIG_PATH,
) -> ValidationPolicy:
    value = read_yaml(path)
    if set(value) != {
        "schema_version",
        "repository",
        "environment_id",
        "location",
        "project_name",
        "telemetry_resource_set",
        "test_agent_model",
        "canary_agents",
        "inventory",
        "limits",
        "trace_hydration",
        "name_policy",
        "resource_kinds",
        "documented_project_cascade",
    }:
        raise ContractError("Test Agent Validation config fields are invalid")
    if value.get("schema_version") != "2.0.0":
        raise ContractError("Test Agent Validation config version is invalid")
    if value.get("repository") != "ninghu/agent-insights-quality":
        raise ContractError("Validation repository is not the reviewed public repository")
    if (
        value.get("environment_id") != "swedencentral-g30"
        or value.get("location") != "swedencentral"
        or value.get("project_name") != "aiq-staging-swedencentral"
        or value.get("telemetry_resource_set") != "g30"
    ):
        raise ContractError("Validation environment is not the reviewed Sweden contract")
    if value.get("test_agent_model") != {
        "deployment_name": "gpt-5.4-mini",
        "model_id": "gpt-5.4-mini",
        "model_version": "2026-03-17",
    }:
        raise ContractError("Validation Test Agent model is not reviewed")
    if value.get("canary_agents") != {
        "prompt": "weather-agent",
        "hosted": "finance-agent",
    }:
        raise ContractError("Validation canary Agents are not reviewed")
    inventory = _mapping(value.get("inventory"), "inventory")
    if inventory != {"agents": 5, "issues": 36, "authorities": 41}:
        raise ContractError("Validation authority inventory is not exact")
    limits_value = _mapping(value.get("limits"), "limits")
    expected_limits = {
        "provisioning_concurrency": 8,
        "telemetry_query_concurrency": 4,
        "runtime_attempt_concurrency": 1,
        "inner_model_call_limit": 4,
        "reserved_capacity_percent": 25,
        "minimum_rpm_headroom": 8,
        "minimum_tpm_headroom": 8192,
        "active_heartbeat_seconds": 60,
        "absolute_ttl_hours": 72,
        "max_recovery_versions_per_agent": 3,
    }
    if limits_value != expected_limits:
        raise ContractError("Validation limits differ from the reviewed policy")
    hydration = _mapping(value.get("trace_hydration"), "trace hydration")
    if hydration != {
        "poll_seconds": 15,
        "stabilization_seconds": 180,
        "maximum_wait_seconds": 900,
    }:
        raise ContractError("Validation trace hydration policy is not reviewed")
    names = _mapping(value.get("name_policy"), "name policy")
    project_names = _name_policy(names.get("project"), "Project")
    agent_names = _name_policy(names.get("agent"), "Agent")
    resource_kinds = value.get("resource_kinds")
    if (
        not isinstance(resource_kinds, list)
        or len(resource_kinds) != len(set(resource_kinds))
        or not all(isinstance(item, str) and item for item in resource_kinds)
    ):
        raise ContractError("Validation resource kinds are invalid")
    cascade = value.get("documented_project_cascade")
    if not isinstance(cascade, list) or any(
        item not in resource_kinds for item in cascade
    ):
        raise ContractError("Validation Project cascade policy is invalid")
    return ValidationPolicy(
        repository=value["repository"],
        environment_id=value["environment_id"],
        location=value["location"],
        project_name=value["project_name"],
        telemetry_resource_set=value["telemetry_resource_set"],
        test_agent_model=dict(value["test_agent_model"]),
        prompt_canary_agent="weather-agent",
        hosted_canary_agent="finance-agent",
        authority_count=inventory["authorities"],
        limits=ValidationLimits(**limits_value),
        project_name_policy=project_names,
        agent_name_policy=agent_names,
        resource_kinds=tuple(resource_kinds),
        documented_project_cascade=tuple(cascade),
        trace_hydration_poll_seconds=hydration["poll_seconds"],
        trace_hydration_stabilization_seconds=hydration["stabilization_seconds"],
        trace_hydration_maximum_wait_seconds=hydration["maximum_wait_seconds"],
    )
def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"Validation {label} must be an object")
    return value


def _name_policy(value: Any, label: str) -> NamePolicy:
    item = _mapping(value, f"{label} name policy")
    if set(item) != {"maximum_length", "pattern"}:
        raise ContractError(f"Validation {label} name policy is invalid")
    maximum = item["maximum_length"]
    pattern = item["pattern"]
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < 8
        or not isinstance(pattern, str)
    ):
        raise ContractError(f"Validation {label} name policy is invalid")
    try:
        re.compile(pattern)
    except re.error as error:
        raise ContractError(
            f"Validation {label} name pattern is invalid"
        ) from error
    return NamePolicy(maximum_length=maximum, pattern=pattern)
