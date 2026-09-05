"""Optional, private email context. Nothing here is an assessment or public report input."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.transport import AZURE_DEVOPS_SCOPE, HttpRequest, Transport


def _query_endpoint(query_url: str) -> tuple[str, str]:
    if not isinstance(query_url, str):
        raise QualityError("work_item_query_invalid")
    try:
        parsed = urlsplit(query_url)
        port = parsed.port
    except ValueError as error:
        raise QualityError("work_item_query_invalid") from error
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        raise QualityError("work_item_query_invalid")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if any(not part or "/" in part or "\\" in part or part in {".", ".."} for part in parts):
        raise QualityError("work_item_query_invalid")
    if parsed.hostname == "dev.azure.com" and len(parts) == 5:
        organization, project, marker, kind, query_id = parts
        base = f"https://dev.azure.com/{quote(organization, safe='')}/{quote(project, safe='')}"
    elif (
        parsed.hostname and re.fullmatch(r"[a-zA-Z0-9-]+\.visualstudio\.com", parsed.hostname)
        and len(parts) == 4
    ):
        project, marker, kind, query_id = parts
        base = f"https://{parsed.hostname}/{quote(project, safe='')}"
    else:
        raise QualityError("work_item_query_invalid")
    if marker != "_queries" or kind != "query" or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", query_id,
    ):
        raise QualityError("work_item_query_invalid")
    return base, f"{base}/_apis/wit/wiql/{query_id}?api-version=7.1"


async def _get(transport: Transport, url: str) -> dict[str, Any]:
    response = await transport.send(HttpRequest("GET", url, scope=AZURE_DEVOPS_SCOPE, timeout=60))
    if response.status != 200:
        raise QualityError("work_item_unavailable", status=response.status)
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeError) as error:
        raise QualityError("work_item_response_invalid") from error
    if not isinstance(value, dict):
        raise QualityError("work_item_response_invalid")
    return value


async def fetch_quality_context(
    query_url: str, report_date: date, transport: Transport,
) -> dict[str, Any]:
    base, endpoint = _query_endpoint(query_url)
    query = await _get(transport, endpoint)
    items = query.get("workItems")
    if not isinstance(items, list):
        raise QualityError("work_item_response_invalid")
    identifiers = []
    for item in items:
        identifier = item.get("id") if isinstance(item, dict) else None
        if type(identifier) is not int or identifier <= 0 or identifier in identifiers:
            raise QualityError("work_item_response_invalid")
        identifiers.append(identifier)
    previous_day = report_date - timedelta(days=1)
    snapshot: dict[str, Any] = {
        "report_date": report_date.isoformat(), "closed_on": previous_day.isoformat(),
        "active": [], "closed": [],
    }
    for start in range(0, len(identifiers), 200):
        batch = identifiers[start:start + 200]
        url = (
            f"{base}/_apis/wit/workitems?ids={','.join(map(str, batch))}"
            "&fields=System.Id,System.Title,System.State,System.Tags,System.AssignedTo,"
            "Microsoft.VSTS.Common.ClosedDate&api-version=7.1"
        )
        response = await _get(transport, url)
        values = response.get("value")
        if (
            not isinstance(values, list) or len(values) != len(batch)
            or any(not isinstance(item, dict) or type(item.get("id")) is not int for item in values)
            or {item["id"] for item in values} != set(batch)
        ):
            raise QualityError("work_item_response_incomplete")
        for item in values:
            fields = item.get("fields")
            if not isinstance(fields, dict):
                raise QualityError("work_item_response_invalid")
            raw_tags = fields.get("System.Tags", "")
            if raw_tags is None:
                raw_tags = ""
            state, title = fields.get("System.State"), fields.get("System.Title")
            if not isinstance(raw_tags, str) or not all(
                isinstance(value, str) and value for value in (state, title)
            ):
                raise QualityError("work_item_response_invalid")
            tags = {value.strip() for value in raw_tags.split(";")}
            if "Quality" not in tags or state.casefold() == "removed":
                continue
            assigned = fields.get("System.AssignedTo")
            owner = assigned.get("displayName", "") if isinstance(assigned, dict) else ""
            if not isinstance(owner, str):
                raise QualityError("work_item_response_invalid")
            entry = {
                "id": item["id"], "title": title,
                "state": state, "owner": owner,
                "url": f"{base}/_workitems/edit/{item['id']}",
            }
            if state.casefold() != "closed":
                snapshot["active"].append(entry)
                continue
            closed = fields.get("Microsoft.VSTS.Common.ClosedDate")
            if not isinstance(closed, str):
                raise QualityError("work_item_closed_date_missing")
            try:
                moment = datetime.fromisoformat(closed.replace("Z", "+00:00"))
            except ValueError as error:
                raise QualityError("work_item_closed_date_invalid") from error
            if moment.tzinfo is None:
                raise QualityError("work_item_closed_date_invalid")
            if moment.astimezone(ZoneInfo("America/Los_Angeles")).date() == previous_day:
                snapshot["closed"].append(entry)
    return snapshot


def render_private_context(snapshot: dict[str, Any]) -> str:
    lines = ["Active Quality work items"]
    for item in snapshot["active"]:
        lines.append(f"{item['id']}: {item['title']} [{item['state']}] {item['owner']} {item['url']}")
    if not snapshot["active"]:
        lines.append("None.")
    lines.append(f"Quality work items closed on {snapshot['closed_on']}")
    for item in snapshot["closed"]:
        lines.append(f"{item['id']}: {item['title']} {item['owner']} {item['url']}")
    if not snapshot["closed"]:
        lines.append("None.")
    return "\n".join(lines)
