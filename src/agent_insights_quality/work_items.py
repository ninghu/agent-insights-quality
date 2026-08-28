from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    read_json,
    runtime_root,
)
_PROGRESS = ProgressReporter("aiq-work-items")

_CLOSED_STATES = {"closed", "completed"}
_ACTIVE_EXCLUDED_STATES = {"removed", *_CLOSED_STATES}
_FIELDS = ("id", "type", "title", "assigned_to", "state", "url")
_BOARD_FIELDS = {
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.Tags",
}


def _query_coordinates(query_url: str) -> tuple[str, str, str, str]:
    parsed = urlparse(query_url)
    parts = [part for part in parsed.path.split("/") if part]
    lowered = [part.casefold() for part in parts]
    if parsed.scheme != "https" or "_queries" not in lowered:
        raise ContractError("Azure Boards query URL is invalid")
    query_index = lowered.index("_queries")
    if (
        len(parts) <= query_index + 2
        or lowered[query_index + 1] != "query"
    ):
        raise ContractError("Azure Boards query URL is invalid")
    query_id = parts[query_index + 2]
    try:
        UUID(query_id)
    except ValueError as error:
        raise ContractError("Azure Boards query identity is invalid") from error
    host = parsed.netloc.casefold()
    if host.endswith(".visualstudio.com") and query_index == 1:
        organization = f"https://{parsed.netloc}/"
        project = unquote(parts[0])
    elif host == "dev.azure.com" and query_index == 2:
        organization = f"https://{parsed.netloc}/{parts[0]}"
        project = unquote(parts[1])
    else:
        raise ContractError("Azure Boards query organization is unsupported")
    marker = parsed.path[: parsed.path.casefold().index("/_queries/")]
    work_item_root = f"{parsed.scheme}://{parsed.netloc}{marker}".rstrip("/")
    return organization, project, query_id, work_item_root


def _assigned_to(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or "").strip() or "Unassigned"
    return str(value or "").strip() or "Unassigned"


def _private_snapshot_path(path: Path) -> Path:
    private_root = runtime_root()
    resolved = path.resolve()
    if not resolved.is_relative_to(private_root):
        raise ContractError("Quality work-item snapshots must stay under .aiq-runtime")
    return resolved


def normalize_quality_work_items(
    values: list[Any],
    query_url: str,
    *,
    closed: bool = False,
    closed_business_date: date | None = None,
) -> list[dict[str, Any]]:
    _, _, _, work_item_root = _query_coordinates(query_url)
    items = []
    for value in values:
        fields = value.get("fields") if isinstance(value, dict) else None
        if not isinstance(fields, dict):
            raise ContractError("Azure Boards returned an invalid work-item shape")
        missing_fields = _BOARD_FIELDS - set(fields)
        if missing_fields:
            raise ContractError(
                "Azure Boards saved query is missing required email columns"
            )
        tags = {
            tag.strip().casefold()
            for tag in str(fields.get("System.Tags") or "").split(";")
            if tag.strip()
        }
        state = str(fields.get("System.State") or "").strip()
        state_key = state.casefold()
        if "quality" not in tags:
            continue
        if closed and state_key not in _CLOSED_STATES:
            continue
        if not closed and state_key in _ACTIVE_EXCLUDED_STATES:
            continue
        if closed:
            closed_at = fields.get("Microsoft.VSTS.Common.ClosedDate")
            if not isinstance(closed_at, str) or not closed_at.strip():
                raise ContractError(
                    "Azure Boards closed item is missing its closed date"
                )
            try:
                closed_at_value = datetime.fromisoformat(
                    closed_at.replace("Z", "+00:00")
                )
            except ValueError as error:
                raise ContractError(
                    "Azure Boards closed item has an invalid closed date"
                ) from error
            if closed_at_value.tzinfo is None:
                raise ContractError(
                    "Azure Boards closed item has an unzoned closed date"
                )
            if (
                closed_business_date is not None
                and closed_at_value.astimezone(
                    ZoneInfo("America/Los_Angeles")
                ).date()
                != closed_business_date
            ):
                continue
        item_id = fields.get("System.Id", value.get("id"))
        if not isinstance(item_id, int) or item_id < 1:
            raise ContractError("Azure Boards work item has an invalid identity")
        work_item_type = str(fields.get("System.WorkItemType") or "").strip()
        title = str(fields.get("System.Title") or "").strip()
        if not work_item_type or not title or not state:
            raise ContractError("Azure Boards work item is missing required fields")
        items.append(
            {
                "id": item_id,
                "type": work_item_type,
                "title": title,
                "assigned_to": _assigned_to(fields.get("System.AssignedTo")),
                "state": state,
                "url": f"{work_item_root}/_workitems/edit/{item_id}",
            }
        )
    return sorted(items, key=lambda item: item["id"])


def _run_boards_query(arguments: list[str]) -> list[Any]:
    command = [
        azure_cli(),
        "boards",
        "query",
        *arguments,
        "--only-show-errors",
        "--output",
        "json",
    ]
    completed = None
    for attempt in range(3):
        try:
            with _PROGRESS.heartbeat(
                f"Azure Boards query attempt {attempt + 1}/3"
            ) as outcome:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if completed.returncode != 0:
                    outcome.fail()
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise ContractError(
                    "Azure Boards query timed out after bounded retries"
                ) from None
            time.sleep(2**attempt)
            continue
        if completed.returncode == 0:
            break
        if attempt < 2:
            time.sleep(2**attempt)
    if completed is None:
        raise ContractError("Azure Boards query retry loop did not execute")
    if completed.returncode != 0:
        raise ContractError("Azure Boards quality work-item query failed")
    if not completed.stdout.strip():
        return []
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("Azure Boards returned invalid JSON") from error
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ContractError("Azure Boards returned an invalid query result")
    return values


def _closed_items_wiql(project: str, closed_date: date) -> str:
    escaped_project = project.replace("'", "''")
    scan_start = closed_date - timedelta(days=1)
    scan_end = closed_date + timedelta(days=2)
    fields = (
        "[System.Id], [System.WorkItemType], [System.Title], "
        "[System.AssignedTo], [System.State], [System.Tags], "
        "[Microsoft.VSTS.Common.ClosedDate]"
    )
    return (
        f"SELECT {fields} FROM WorkItems "
        f"WHERE [System.TeamProject] = '{escaped_project}' "
        "AND [System.Tags] CONTAINS 'Quality' "
        "AND [System.State] IN ('Closed', 'Completed') "
        "AND [Microsoft.VSTS.Common.ClosedDate] >= "
        f"'{scan_start.isoformat()}' "
        "AND [Microsoft.VSTS.Common.ClosedDate] < "
        f"'{scan_end.isoformat()}' ORDER BY [System.Id]"
    )


def fetch_quality_work_items(
    query_url: str,
    report_date: date,
    output: Path,
) -> int:
    resolved_output = _private_snapshot_path(output)
    organization, project, query_id, _ = _query_coordinates(query_url)
    active_values = _run_boards_query(
        [
            "--id",
            query_id,
            "--organization",
            organization,
            "--project",
            project,
        ]
    )
    closed_date = report_date - timedelta(days=1)
    wiql = _closed_items_wiql(project, closed_date)
    closed_values = _run_boards_query(
        [
            "--wiql",
            wiql,
            "--organization",
            organization,
            "--project",
            project,
        ]
    )
    active_items = normalize_quality_work_items(active_values, query_url)
    closed_items = normalize_quality_work_items(
        closed_values,
        query_url,
        closed=True,
        closed_business_date=closed_date,
    )
    atomic_json(
        resolved_output,
        {
            "schema_version": "2.0.0",
            "query_reference": content_hash({"query_url": query_url}),
            "closed_business_date": closed_date.isoformat(),
            "active_items": active_items,
            "closed_yesterday_items": closed_items,
        },
    )
    return len(active_items) + len(closed_items)


def _validate_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ContractError("Quality work-item snapshot is invalid")
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != set(_FIELDS)
            or not isinstance(item["id"], int)
            or item["id"] < 1
            or any(
                not isinstance(item[field], str) or not item[field].strip()
                for field in _FIELDS[1:]
            )
            or urlparse(item["url"]).scheme != "https"
        ):
            raise ContractError("Quality work-item snapshot contains invalid data")
    if [item["id"] for item in items] != sorted(
        {item["id"] for item in items}
    ):
        raise ContractError("Quality work-item snapshot identities are not unique and sorted")
    return items


def load_quality_work_items(
    path: Path,
    *,
    report_date: date | None = None,
) -> dict[str, Any]:
    value = read_json(_private_snapshot_path(path))
    if (
        value.get("schema_version") != "2.0.0"
        or set(value)
        != {
            "schema_version",
            "query_reference",
            "closed_business_date",
            "active_items",
            "closed_yesterday_items",
        }
    ):
        raise ContractError("Quality work-item snapshot is invalid")
    try:
        closed_date = date.fromisoformat(str(value["closed_business_date"]))
    except ValueError as error:
        raise ContractError("Quality work-item closed date is invalid") from error
    if report_date is not None and closed_date != report_date - timedelta(days=1):
        raise ContractError("Quality work-item snapshot does not match the report date")
    active = _validate_items(value["active_items"])
    if any(
        item["state"].casefold() in _ACTIVE_EXCLUDED_STATES for item in active
    ):
        raise ContractError("Active quality work-item snapshot contains closed data")
    closed = _validate_items(value["closed_yesterday_items"])
    if any(item["state"].casefold() not in _CLOSED_STATES for item in closed):
        raise ContractError("Closed quality work-item snapshot contains active data")
    return value
