"""Private work-item snapshots; never inputs to assessment or public publication.

The strict email context schema is ``{schema_version: "1.0", status:
"available", snapshot: {report_date, query_url, window, active, closed}}`` or
``{schema_version: "1.0", status: "unavailable", code, window: window | None}``.
Every entry has exactly ``id, type, title, state, owner, url``. Empty owner means
unassigned, not a missing fetch. URLs must match the saved query's organization,
project and item ID.

A window has exactly ``start, end, timezone, basis, previous_delivery_id``.
Its timestamps are canonical UTC ISO instants; timezone is America/Los_Angeles.
Closed dates use [start, end), where end is the query provider's asOf cutoff.
The basis is previous_official_report (with a delivery ID) or initial_lookback
(no delivery ID, exactly seven elapsed days). Query scope is never expanded.
Only integration's immutable private checkpoints freeze these dictionaries.

Explicit local restyling can also supply ``schema_version: "legacy-1.0"`` with
``status: "available"`` and the unchanged legacy snapshot: exactly report_date,
closed_on, active and closed, whose entries omit type. This is not a new-window
snapshot or an official comparison anchor. Type is displayed as "Not recorded";
closed_on remains the recorded America/Los_Angeles calendar date.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.transport import AZURE_DEVOPS_SCOPE, HttpRequest, Transport

WINDOW_TIMEZONE = "America/Los_Angeles"
_WINDOW_KEYS = {"start", "end", "timezone", "basis", "previous_delivery_id"}
_ENTRY_KEYS = {"id", "type", "title", "state", "owner", "url"}


def _moment(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        raise QualityError(code)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("Timezone required")
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise QualityError(code) from error


def _text(value: Any, *, empty: bool = False, limit: int = 2048) -> bool:
    return (
        isinstance(value, str) and len(value) <= limit and value == value.strip()
        and (value == "" and empty or bool(value) and value.isprintable())
    )


def _ado_location(url: str, code: str) -> tuple[str, list[str]]:
    if not _text(url, limit=8192):
        raise QualityError(code)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise QualityError(code) from error
    if (
        parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
        or port is not None
        or parsed.query or parsed.fragment
    ):
        raise QualityError(code)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if any(
        not _text(part, limit=256) or "/" in part or "\\" in part or part in {".", ".."}
        for part in parts
    ):
        raise QualityError(code)
    if parsed.hostname == "dev.azure.com" and len(parts) >= 2:
        organization, project = parts[:2]
        base = f"https://dev.azure.com/{quote(organization, safe='')}/{quote(project, safe='')}"
        tail = parts[2:]
    elif (
        parsed.hostname and re.fullmatch(r"[a-zA-Z0-9-]+\.visualstudio\.com", parsed.hostname)
        and len(parts) >= 1
    ):
        project = parts[0]
        base = f"https://{parsed.hostname}/{quote(project, safe='')}"
        tail = parts[1:]
    else:
        raise QualityError(code)
    return base, tail


def _query_endpoint(query_url: str) -> tuple[str, str]:
    base, tail = _ado_location(query_url, "work_item_query_invalid")
    if len(tail) != 3 or tail[:2] != ["_queries", "query"] or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", tail[2],
    ):
        raise QualityError("work_item_query_invalid")
    return base, f"{base}/_apis/wit/wiql/{tail[2]}?api-version=7.1"


def validate_window(window: Any) -> dict:
    code = "work_item_window_invalid"
    if not isinstance(window, dict) or set(window) != _WINDOW_KEYS:
        raise QualityError(code)
    start, end = (_moment(window[key], code) for key in ("start", "end"))
    if (
        window["start"] != start.isoformat() or window["end"] != end.isoformat()
        or start >= end or window["timezone"] != WINDOW_TIMEZONE
    ):
        raise QualityError(code)
    if window["basis"] == "initial_lookback":
        if window["previous_delivery_id"] is not None or end - start != timedelta(days=7):
            raise QualityError(code)
    elif window["basis"] == "previous_official_report":
        identifier = window["previous_delivery_id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", identifier):
            raise QualityError(code)
    else:
        raise QualityError(code)
    return window


def unavailable_context(code: str, *, window: dict | None = None) -> dict:
    value = {"schema_version": "1.0", "status": "unavailable", "code": code, "window": window}
    validate_work_item_context(value)
    return value


def validate_work_item_context(context: Any) -> dict:
    """Validate all private values before rendering; never coerce malformed data."""
    code = "work_item_context_invalid"
    if not isinstance(context, dict) or context.get("schema_version") not in ("1.0", "legacy-1.0"):
        raise QualityError(code)
    legacy = context["schema_version"] == "legacy-1.0"
    if context.get("status") == "unavailable":
        if (
            legacy or set(context) != {"schema_version", "status", "code", "window"}
            or not isinstance(context["code"], str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", context["code"]) is None
        ):
            raise QualityError(code)
        if context["window"] is not None:
            validate_window(context["window"])
        return context
    if context.get("status") != "available" or set(context) != {"schema_version", "status", "snapshot"}:
        raise QualityError(code)
    snapshot = context["snapshot"]
    expected = (
        {"report_date", "closed_on", "active", "closed"} if legacy
        else {"report_date", "query_url", "window", "active", "closed"}
    )
    if not isinstance(snapshot, dict) or set(snapshot) != expected:
        raise QualityError(code)
    try:
        for key in ("report_date", "closed_on") if legacy else ("report_date",):
            if date.fromisoformat(snapshot[key]).isoformat() != snapshot[key]:
                raise ValueError("Canonical date required")
    except (ValueError, TypeError) as error:
        raise QualityError(code) from error
    base = None if legacy else _query_endpoint(snapshot["query_url"])[0]
    if not legacy:
        validate_window(snapshot["window"])
    seen = set()
    for group in ("active", "closed"):
        entries = snapshot[group]
        if not isinstance(entries, list) or len(entries) > 20_000:
            raise QualityError(code)
        for item in entries:
            if not isinstance(item, dict) or set(item) != (_ENTRY_KEYS - {"type"} if legacy else _ENTRY_KEYS):
                raise QualityError(code)
            identifier = item["id"]
            if (
                type(identifier) is not int or not 0 < identifier <= 2_147_483_647
                or identifier in seen
                or not _text(item["title"])
                or not legacy and not _text(item["type"], limit=256)
                or not _text(item["state"], limit=128)
                or not _text(item["owner"], empty=True, limit=512)
                or item["state"].casefold() == "removed"
                or (item["state"].casefold() == "closed") != (group == "closed")
            ):
                raise QualityError(code)
            if legacy:
                item_base, tail = _ado_location(item["url"], code)
                if tail != ["_workitems", "edit", str(identifier)]:
                    raise QualityError(code)
                if base is None:
                    base = item_base
                elif base != item_base:
                    raise QualityError(code)
            if item["url"] != f"{base}/_workitems/edit/{identifier}":
                raise QualityError(code)
            seen.add(identifier)
    return context


def legacy_work_item_email_context(
    checkpoint: dict, private_context: str | None,
) -> tuple[dict, str | None]:
    """Normalize an already-retained same-run checkpoint for explicit local preview.

    Callers must read only that run's completed work-item-context, never refetch
    or derive entries from prose. No checkpoint is modified. Only the exact saved
    text prefix is removed; any assessor suffix is preserved byte-for-byte.
    """
    if private_context is not None and not isinstance(private_context, str):
        raise QualityError("work_item_legacy_context_invalid")
    if not isinstance(checkpoint, dict):
        raise QualityError("work_item_legacy_context_invalid")
    if checkpoint.get("status") == "unavailable":
        if set(checkpoint) != {"status", "code", "text"} or checkpoint["text"] is not None:
            raise QualityError("work_item_legacy_context_invalid")
        return unavailable_context(checkpoint["code"]), private_context
    if (
        set(checkpoint) != {"status", "snapshot", "text"} or checkpoint["status"] != "available"
        or not isinstance(checkpoint["text"], str) or not checkpoint["text"]
    ):
        raise QualityError("work_item_legacy_context_invalid")
    context = validate_work_item_context({
        "schema_version": "legacy-1.0", "status": "available", "snapshot": checkpoint["snapshot"],
    })
    if private_context is not None and private_context.startswith(checkpoint["text"]):
        private_context = private_context[len(checkpoint["text"]):] or None
    return deepcopy(context), private_context


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
    *, previous_snapshot: dict | None = None,
) -> dict[str, Any]:
    """Fetch the configured query only, then read every item at its provider cutoff.

    ``previous_snapshot`` is integration's verified official delivery boundary:
    exactly ``{delivery_id, cutoff}``, or None when no sent snapshot exists.
    A missing provider asOf is unavailable, never a guessed wall-clock snapshot.
    """
    base, endpoint = _query_endpoint(query_url)
    if previous_snapshot is not None and (
        not isinstance(previous_snapshot, dict)
        or set(previous_snapshot) != {"delivery_id", "cutoff"}
    ):
        raise QualityError("work_item_anchor_invalid")
    query = await _get(transport, endpoint)
    cutoff = _moment(query.get("asOf"), "work_item_snapshot_cutoff_missing")
    start = (
        _moment(previous_snapshot["cutoff"], "work_item_anchor_invalid")
        if previous_snapshot is not None else cutoff - timedelta(days=7)
    )
    window = validate_window({
        "start": start.isoformat(), "end": cutoff.isoformat(), "timezone": WINDOW_TIMEZONE,
        "basis": "previous_official_report" if previous_snapshot is not None else "initial_lookback",
        "previous_delivery_id": previous_snapshot["delivery_id"] if previous_snapshot is not None else None,
    })
    items = query.get("workItems")
    if (
        not isinstance(items, list) or len(items) > 20_000
        or query.get("queryType", "flat") != "flat"
    ):
        raise QualityError("work_item_response_invalid")
    identifiers = []
    for item in items:
        identifier = item.get("id") if isinstance(item, dict) else None
        if type(identifier) is not int or not 0 < identifier <= 2_147_483_647 or identifier in identifiers:
            raise QualityError("work_item_response_invalid")
        identifiers.append(identifier)
    snapshot: dict[str, Any] = {
        "report_date": report_date.isoformat(), "query_url": query_url, "window": window,
        "active": [], "closed": [],
    }
    for batch_start in range(0, len(identifiers), 200):
        batch = identifiers[batch_start:batch_start + 200]
        url = (
            f"{base}/_apis/wit/workitems?ids={','.join(map(str, batch))}"
            "&fields=System.Id,System.WorkItemType,System.Title,System.State,System.Tags,System.AssignedTo,"
            "Microsoft.VSTS.Common.ClosedDate&asOf=" + quote(query["asOf"], safe="") + "&api-version=7.1"
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
            state, title, kind = (
                fields.get("System.State"), fields.get("System.Title"), fields.get("System.WorkItemType")
            )
            if not isinstance(raw_tags, str) or not all(_text(value) for value in (state, title, kind)):
                raise QualityError("work_item_response_invalid")
            tags = {value.strip() for value in raw_tags.split(";")}
            if "Quality" not in tags or state.casefold() == "removed":
                continue
            assigned = fields.get("System.AssignedTo")
            if assigned is None:
                owner = ""
            elif isinstance(assigned, dict) and _text(assigned.get("displayName"), limit=512):
                owner = assigned["displayName"]
            else:
                raise QualityError("work_item_response_invalid")
            entry = {
                "id": item["id"], "type": kind, "title": title,
                "state": state, "owner": owner,
                "url": f"{base}/_workitems/edit/{item['id']}",
            }
            if state.casefold() != "closed":
                snapshot["active"].append(entry)
                continue
            closed = fields.get("Microsoft.VSTS.Common.ClosedDate")
            if not isinstance(closed, str):
                raise QualityError("work_item_closed_date_missing")
            moment = _moment(closed, "work_item_closed_date_invalid")
            if start <= moment < cutoff:
                snapshot["closed"].append(entry)
    validate_work_item_context({"schema_version": "1.0", "status": "available", "snapshot": snapshot})
    return snapshot


def _window_label(window: dict) -> str:
    zone = ZoneInfo(window["timezone"])
    start, end = (
        _moment(window[key], "work_item_window_invalid").astimezone(zone).isoformat(sep=" ")
        for key in ("start", "end")
    )
    basis = (
        "Since the previous successfully submitted official report snapshot"
        if window["basis"] == "previous_official_report"
        else "Initial 7-day lookback; no prior successfully submitted official snapshot is available"
    )
    return f"{basis}. {start} (inclusive) to {end} (exclusive), {window['timezone']}."


def render_private_context(snapshot: dict[str, Any]) -> str:
    validate_work_item_context({"schema_version": "1.0", "status": "available", "snapshot": snapshot})
    lines = [_window_label(snapshot["window"]), "Active Quality work items"]
    for item in snapshot["active"]:
        lines.append(f"{item['id']}: {item['type']} - {item['title']} [{item['state']}] {item['owner']} {item['url']}")
    if not snapshot["active"]:
        lines.append("None.")
    lines.append("Closed Quality work items in the snapshot window")
    for item in snapshot["closed"]:
        lines.append(f"{item['id']}: {item['type']} - {item['title']} {item['owner']} {item['url']}")
    if not snapshot["closed"]:
        lines.append("None.")
    return "\n".join(lines)


def render_work_item_html(context: dict | None) -> str:
    """Return private-only section contents, without an enclosing card/table row."""
    if context is None:
        return ""
    validate_work_item_context(context)
    text_style = "font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#12304a;"
    section = [
        '<h2 style="font-family:Segoe UI,Arial,sans-serif;font-size:20px;line-height:27px;'
        'color:#12304a;margin:0 0 12px;">Quality work items</h2>'
    ]
    if context["status"] == "unavailable":
        detail = {
            "work_item_legacy_snapshot": (
                "The retained historical snapshot has no typed reporting window. "
                "Historical text, when present, is unchanged; no refreshed table was substituted."
            ),
            "work_item_anchor_metadata_missing": "Previous official snapshot metadata is missing.",
            "work_item_anchor_metadata_invalid": "Previous official snapshot metadata is invalid.",
            "work_item_anchor_legacy": "Previous official snapshots do not record an exact cutoff.",
            "work_item_context_interrupted": "The snapshot fetch was interrupted and has not been repeated.",
        }.get(context["code"], "The optional work-item snapshot could not be established.")
        section.append(
            f'<p style="{text_style}margin:0;background-color:#e8eef7;'
            'padding:10px;border:1px solid #d6deea;">'
            "<strong>Unavailable.</strong> " + escape(detail) + " This is not an empty result.</p>"
        )
        if context["window"] is not None:
            section.append(f'<p style="{text_style}margin:8px 0;">' + escape(_window_label(context["window"])) + "</p>")
    else:
        snapshot = context["snapshot"]
        legacy = context["schema_version"] == "legacy-1.0"
        note = (
            f"Retained legacy snapshot for report {snapshot['report_date']}. "
            f"Closed items were recorded for {snapshot['closed_on']} "
            f"({WINDOW_TIMEZONE} calendar date). Type, an exact snapshot cutoff and a "
            "since-previous-report window were not recorded. No refresh was performed."
            if legacy else _window_label(snapshot["window"])
        )
        section.append(f'<p style="{text_style}margin:0 0 10px;">' + escape(note) + "</p>")
        if not legacy:
            section.append(
                f'<p style="{text_style}margin:0 0 10px;">'
                'Only items returned by the configured Quality query are included.</p>'
            )
        closed_title = f"Closed on {snapshot['closed_on']}" if legacy else "Closed in this window"
        for key, title in (("active", "Active"), ("closed", closed_title)):
            section.append(
                '<h3 style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;'
                'margin:12px 0 6px;color:#12304a;">' + title + "</h3>"
            )
            entries = snapshot[key]
            if not entries:
                section.append(f'<p style="{text_style}margin:0 0 8px;">None in this snapshot.</p>')
                continue
            section.append(
                '<table cellpadding="0" cellspacing="0" width="100%" style="table-layout:fixed;border-collapse:collapse;'
                'font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#12304a;">'
                '<colgroup><col style="width:8%"><col style="width:12%"><col style="width:40%">'
                '<col style="width:25%"><col style="width:15%"></colgroup>'
                "<thead><tr>"
            )
            for heading in ("ID", "Type", "Title", "Owner", "State"):
                section.append(
                    '<th scope="col" align="left" style="padding:7px;border:1px solid #d6deea;'
                    'background-color:#e8eef7;color:#12304a;">' + heading + "</th>"
                )
            section.append("</tr></thead><tbody>")
            for item in entries:
                section.append("<tr>")
                cells = [
                    '<a style="color:#12304a;text-decoration:underline;" href="'
                    + escape(item["url"], quote=True) + '">' + str(item["id"]) + "</a>",
                    "Not recorded" if legacy else escape(item["type"]),
                    escape(item["title"]), escape(item["owner"] or "Unassigned"),
                    escape(item["state"]),
                ]
                for cell in cells:
                    section.append(
                        '<td valign="top" style="padding:10px;border:1px solid #d6deea;'
                        'overflow-wrap:anywhere;word-wrap:break-word;">' + cell + "</td>"
                    )
                section.append("</tr>")
            section.append("</tbody></table>")
    return "".join(section)
