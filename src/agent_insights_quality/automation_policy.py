from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_insights_quality.selection import DAILY_ISSUES_PER_AGENT
from agent_insights_quality.util import ROOT, ContractError, read_yaml

FIXED_TELEMETRY_RESOURCE_SET = "g30"
FIXED_STORAGE_ACCOUNT_PREFIX = "aiqsweart"
FIXED_STORAGE_RESOURCE_ROLE = "qualification-storage"
FIXED_QUALITY_ARTIFACT_CONTAINER = "quality-artifacts"
FIXED_DEPLOYMENT_REGISTRY_CONTAINER = "deployment-registries"
MINIMUM_LOOKBACK_HOURS = 0.1
TRAFFIC_UNCERTAINTY_SECONDS = 10 * 60
TRACE_ASSERTION_DEADLINE_SECONDS = 15 * 60
TRACE_ASSERTION_POLL_SECONDS = 15


@dataclass(frozen=True)
class AutomationPolicy:
    issues_per_agent_daily: int
    max_parallel_agents: int
    daily_inter_version_pacing_seconds: int
    insight_lookback_hours: float
    insight_lookback_max_hours: float
    insight_lookback_precision_minutes: int
    clean_window_poll_seconds: int
    clean_window_ingestion_margin_seconds: int
    clean_window_max_wait_seconds: int
    trace_assertion_stabilization_seconds: int
    daily_trace_hydration_grace_seconds: int
    daily_trace_hydration_maximum_wait_seconds: int
    daily_trace_hydration_maximum_poll_seconds: int
    insight_start_margin_seconds: int
    max_recovery_versions: int
    agent_start_stagger_seconds: int
    telemetry_resource_set: str
    storage_account_prefix: str
    storage_resource_role: str
    quality_artifact_container: str
    deployment_registry_container: str


def load_automation_policy(
    path: Path = ROOT / "config" / "automation.yaml",
) -> AutomationPolicy:
    value = read_yaml(path)
    if value.get("schema_version") != "4.0.0":
        raise ContractError("Automation policy schema version is invalid")
    daily_issues = _positive_int(
        value.get("issues_per_agent_daily"),
        "daily issue count",
    )
    if daily_issues != DAILY_ISSUES_PER_AGENT:
        raise ContractError("Automation daily issue count is not the reviewed value")
    max_parallel_agents = _positive_int(
        value.get("max_parallel_agents"),
        "parallel Agent limit",
    )
    if max_parallel_agents > 5:
        raise ContractError("Automation parallel Agent limit exceeds the fixed inventory")
    pacing_seconds = _positive_int(
        value.get("daily_inter_version_pacing_seconds"),
        "Daily inter-version pacing",
    )
    if pacing_seconds != 60:
        raise ContractError("Daily inter-version pacing must be 60 seconds")
    lookback = _finite_number(value.get("insight_lookback_hours"), "lookback")
    if lookback < MINIMUM_LOOKBACK_HOURS:
        raise ContractError("Automation lookback is below the reviewed minimum")
    maximum_lookback = _finite_number(
        value.get("insight_lookback_max_hours"),
        "maximum lookback",
    )
    if maximum_lookback < lookback or maximum_lookback > 24:
        raise ContractError("Automation maximum lookback is outside the reviewed bounds")
    lookback_precision = _positive_int(
        value.get("insight_lookback_precision_minutes"),
        "lookback precision",
    )
    if lookback_precision != 1:
        raise ContractError("Automation lookback precision must be one minute")
    poll = _positive_int(value.get("clean_window_poll_seconds"), "poll interval")
    margin = _nonnegative_int(
        value.get("clean_window_ingestion_margin_seconds"),
        "ingestion margin",
    )
    maximum_wait = _positive_int(
        value.get("clean_window_max_wait_seconds"),
        "maximum clean-window wait",
    )
    assertion_stabilization = _positive_int(
        value.get("trace_assertion_stabilization_seconds"),
        "trace assertion stabilization interval",
    )
    hydration_grace = _nonnegative_int(
        value.get("daily_trace_hydration_grace_seconds"),
        "Daily trace hydration grace",
    )
    hydration_maximum = _positive_int(
        value.get("daily_trace_hydration_maximum_wait_seconds"),
        "Daily trace hydration maximum",
    )
    hydration_maximum_poll = _positive_int(
        value.get("daily_trace_hydration_maximum_poll_seconds"),
        "Daily trace hydration maximum poll",
    )
    start_margin = _positive_int(
        value.get("insight_start_margin_seconds"),
        "Insight start margin",
    )
    if maximum_wait < TRAFFIC_UNCERTAINTY_SECONDS + lookback * 3600 + margin:
        raise ContractError(
            "Maximum clean-window wait is shorter than the uncertainty horizon"
        )
    if assertion_stabilization != 2 * TRACE_ASSERTION_POLL_SECONDS:
        raise ContractError(
            "Trace assertion stabilization interval must use the reviewed "
            "two-snapshot duration"
        )
    if assertion_stabilization >= TRACE_ASSERTION_DEADLINE_SECONDS:
        raise ContractError(
            "Trace assertion stabilization interval must fit within its bounded deadline"
        )
    if hydration_maximum < 60 or hydration_maximum > 5 * 60:
        raise ContractError(
            "Daily trace hydration maximum is outside the reviewed horizon"
        )
    if hydration_grace >= hydration_maximum:
        raise ContractError("Daily trace hydration grace exceeds its horizon")
    if hydration_maximum_poll < poll or hydration_maximum_poll > 60:
        raise ContractError(
            "Daily trace hydration maximum poll is outside reviewed bounds"
        )
    if start_margin + TRACE_ASSERTION_POLL_SECONDS >= lookback * 3600:
        raise ContractError(
            "Insight start margin leaves no time to observe activation within lookback"
        )
    recoveries = _nonnegative_int(
        value.get("max_recovery_versions"),
        "recovery limit",
    )
    if recoveries > 3:
        raise ContractError("Automation recovery limit exceeds the reviewed maximum")
    stagger = _nonnegative_int(
        value.get("agent_start_stagger_seconds"),
        "Agent start stagger",
    )
    if stagger > 30:
        raise ContractError("Automation Agent start stagger is unreasonably long")
    resource_set = str(value.get("telemetry_resource_set") or "")
    if (
        re.fullmatch(r"g[1-9][0-9]*", resource_set) is None
        or resource_set != FIXED_TELEMETRY_RESOURCE_SET
    ):
        raise ContractError("Automation telemetry resource set is not the fixed reviewed set")
    storage_account_prefix = str(value.get("storage_account_prefix") or "")
    if storage_account_prefix != FIXED_STORAGE_ACCOUNT_PREFIX:
        raise ContractError("Automation storage account prefix is not the reviewed value")
    storage_resource_role = str(value.get("storage_resource_role") or "")
    if storage_resource_role != FIXED_STORAGE_RESOURCE_ROLE:
        raise ContractError("Automation storage resource role is not the reviewed value")
    quality_artifact_container = str(value.get("quality_artifact_container") or "")
    if quality_artifact_container != FIXED_QUALITY_ARTIFACT_CONTAINER:
        raise ContractError("Automation quality-artifact container is not reviewed")
    deployment_registry_container = str(
        value.get("deployment_registry_container") or ""
    )
    if deployment_registry_container != FIXED_DEPLOYMENT_REGISTRY_CONTAINER:
        raise ContractError("Automation deployment-registry container is not reviewed")
    return AutomationPolicy(
        issues_per_agent_daily=daily_issues,
        max_parallel_agents=max_parallel_agents,
        daily_inter_version_pacing_seconds=pacing_seconds,
        insight_lookback_hours=lookback,
        insight_lookback_max_hours=maximum_lookback,
        insight_lookback_precision_minutes=lookback_precision,
        clean_window_poll_seconds=poll,
        clean_window_ingestion_margin_seconds=margin,
        clean_window_max_wait_seconds=maximum_wait,
        trace_assertion_stabilization_seconds=assertion_stabilization,
        daily_trace_hydration_grace_seconds=hydration_grace,
        daily_trace_hydration_maximum_wait_seconds=hydration_maximum,
        daily_trace_hydration_maximum_poll_seconds=hydration_maximum_poll,
        insight_start_margin_seconds=start_margin,
        max_recovery_versions=recoveries,
        agent_start_stagger_seconds=stagger,
        telemetry_resource_set=resource_set,
        storage_account_prefix=storage_account_prefix,
        storage_resource_role=storage_resource_role,
        quality_artifact_container=quality_artifact_container,
        deployment_registry_container=deployment_registry_container,
    )


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"Automation {label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"Automation {label} must be finite")
    return result


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise ContractError(f"Automation {label} must be positive")
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"Automation {label} must be a nonnegative integer")
    return value
