from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_insights_quality.selection import DAILY_ISSUES_PER_AGENT
from agent_insights_quality.util import ROOT, ContractError, read_yaml

FIXED_TELEMETRY_RESOURCE_SET = "g29"
MINIMUM_LOOKBACK_HOURS = 0.1
TRAFFIC_UNCERTAINTY_SECONDS = 10 * 60
TRACE_ASSERTION_DEADLINE_SECONDS = 15 * 60
TRACE_ASSERTION_POLL_SECONDS = 15


@dataclass(frozen=True)
class AutomationPolicy:
    issues_per_agent_daily: int
    insight_lookback_hours: float
    clean_window_poll_seconds: int
    clean_window_ingestion_margin_seconds: int
    clean_window_max_wait_seconds: int
    trace_assertion_stabilization_seconds: int
    insight_start_margin_seconds: int
    max_recovery_versions: int
    agent_start_stagger_seconds: int
    telemetry_resource_set: str


def load_automation_policy(
    path: Path = ROOT / "config" / "automation.yaml",
) -> AutomationPolicy:
    value = read_yaml(path)
    if value.get("schema_version") != "2.0.0":
        raise ContractError("Automation policy schema version is invalid")
    daily_issues = _positive_int(
        value.get("issues_per_agent_daily"),
        "daily issue count",
    )
    if daily_issues != DAILY_ISSUES_PER_AGENT:
        raise ContractError("Automation daily issue count is not the reviewed value")
    lookback = _finite_number(value.get("insight_lookback_hours"), "lookback")
    if lookback < MINIMUM_LOOKBACK_HOURS:
        raise ContractError("Automation lookback is below the reviewed minimum")
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
    start_margin = _positive_int(
        value.get("insight_start_margin_seconds"),
        "Insight start margin",
    )
    if maximum_wait < TRAFFIC_UNCERTAINTY_SECONDS + lookback * 3600 + margin:
        raise ContractError(
            "Maximum clean-window wait is shorter than the uncertainty horizon"
        )
    if assertion_stabilization <= 2 * TRACE_ASSERTION_POLL_SECONDS + margin:
        raise ContractError(
            "Trace assertion stabilization interval is shorter than the reviewed "
            "ingestion margin"
        )
    if assertion_stabilization >= TRACE_ASSERTION_DEADLINE_SECONDS:
        raise ContractError(
            "Trace assertion stabilization interval must fit within its bounded deadline"
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
    return AutomationPolicy(
        issues_per_agent_daily=daily_issues,
        insight_lookback_hours=lookback,
        clean_window_poll_seconds=poll,
        clean_window_ingestion_margin_seconds=margin,
        clean_window_max_wait_seconds=maximum_wait,
        trace_assertion_stabilization_seconds=assertion_stabilization,
        insight_start_margin_seconds=start_margin,
        max_recovery_versions=recoveries,
        agent_start_stagger_seconds=stagger,
        telemetry_resource_set=resource_set,
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
