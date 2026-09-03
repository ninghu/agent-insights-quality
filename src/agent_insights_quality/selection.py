from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from agent_insights_quality.util import ContractError

EPOCH_MONDAY = date(2026, 1, 5)
DAILY_ISSUES_PER_AGENT = 4
DAILY_ISSUE_COUNT = 20
STAGING_ISSUE_COUNT = 36


def _business_days_since_epoch(value: date) -> int:
    if value.weekday() >= 5:
        raise ContractError("Daily qualification runs Monday through Friday")
    if value < EPOCH_MONDAY:
        raise ContractError("Report date predates the reviewed selection epoch")
    days = 0
    cursor = EPOCH_MONDAY
    while cursor < value:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _permutation(agent_name: str, issue_ids: list[str], issue_hash: str) -> list[str]:
    return sorted(
        issue_ids,
        key=lambda issue_id: hashlib.sha256(
            f"{issue_hash}:{agent_name}:{issue_id}".encode("ascii")
        ).hexdigest(),
    )


def select_daily(
    report_date: date,
    agents: dict[str, Any],
    issues: dict[str, Any],
    issue_hash: str,
) -> dict[str, list[str]]:
    day_index = _business_days_since_epoch(report_date)
    count = int(issues["selection"]["issues_per_agent_daily"])
    if count != DAILY_ISSUES_PER_AGENT:
        raise ContractError("Daily issue selection count is not the reviewed value")
    selected: dict[str, list[str]] = {}
    for agent in agents["agents"]:
        ordered = _permutation(agent["name"], list(agent["issue_ids"]), issue_hash)
        if len(ordered) < count:
            raise ContractError(
                f"{agent['name']} has fewer issues than the daily selection count"
            )
        start = (day_index * count) % len(ordered)
        selected[agent["name"]] = [
            ordered[(start + offset) % len(ordered)] for offset in range(count)
        ]
    return selected


def select_full(agents: dict[str, Any]) -> dict[str, list[str]]:
    return {agent["name"]: list(agent["issue_ids"]) for agent in agents["agents"]}
