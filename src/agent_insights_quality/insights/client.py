from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.receipts import MonitorOwnershipRegistry

_API_VERSION = "2025-05-15-preview"
_TERMINAL = {"succeeded", "failed", "cancelled", "canceled"}


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as error:
            raise RuntimeFailure(
                "invalid_agent_insights_response",
                "Agent Insights returned malformed JSON.",
            ) from error


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout: float,
    ) -> HttpResponse: ...


class UrlLibTransport:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(self._NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout: float,
    ) -> HttpResponse:
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, dict(error.headers), error.read())
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeFailure(
                "agent_insights_unavailable",
                "Agent Insights request failed before receiving a response.",
                transient=True,
            ) from error


class TokenCredential(Protocol):
    def get_token(self, *scopes: str) -> Any: ...


def _segment(value: str, label: str) -> str:
    if not value or len(value) > 256 or "/" in value or "\\" in value or "\x00" in value:
        raise RuntimeFailure("invalid_agent_insights_identifier", f"{label} is invalid.")
    return urllib.parse.quote(value, safe="")


def _items(payload: Any, label: str) -> list[Mapping[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
        raise RuntimeFailure("invalid_agent_insights_response", f"{label} response was invalid.")
    return list(data)


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeFailure("invalid_agent_insights_response", f"{label} timestamp is missing.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeFailure(
            "invalid_agent_insights_response",
            f"{label} timestamp is invalid.",
        ) from error
    if parsed.tzinfo is None:
        raise RuntimeFailure("invalid_agent_insights_response", f"{label} timestamp has no timezone.")
    return parsed.astimezone(UTC)


def _field(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return None


@dataclass(frozen=True, slots=True)
class InsightCheckpoint:
    captured_at: datetime
    revisions: Mapping[str, str]
    details: Mapping[str, Mapping[str, Any]] | None = None


class AgentInsightsClient:
    def __init__(
        self,
        project_endpoint: str,
        credential: TokenCredential,
        *,
        transport: Transport | None = None,
        ownership_registry: MonitorOwnershipRegistry | None = None,
        timeout_seconds: float = 60,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urllib.parse.urlparse(project_endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise RuntimeFailure("invalid_project_endpoint", "Project endpoint must be an HTTPS base URL.")
        self._base = project_endpoint.rstrip("/")
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._credential = credential
        self._transport = transport or UrlLibTransport()
        self._ownership = ownership_registry
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        query = {"api-version": _API_VERSION}
        query.update({key: value for key, value in (params or {}).items() if value is not None})
        return f"{self._base}{path}?{urllib.parse.urlencode(query)}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        expected: set[int] | None = None,
    ) -> tuple[int, Any]:
        url = self._url(path, params)
        parsed = urllib.parse.urlparse(url)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != self._origin
            or not url.startswith(self._base + "/")
        ):
            raise RuntimeFailure("invalid_pagination_link", "Pagination link changed endpoint origin.")
        token = str(self._credential.get_token("https://ai.azure.com/.default").token)
        if not token:
            raise RuntimeFailure("missing_access_token", "Azure credential returned an empty token.")
        response = self._transport.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json_body=json_body,
            timeout=self._timeout,
        )
        if response.status not in (expected or {200}):
            request_id = response.headers.get("x-ms-request-id") or response.headers.get("request-id")
            raise RuntimeFailure(
                "agent_insights_request_failed",
                "Agent Insights API request failed.",
                {
                    "method": method,
                    "path": path,
                    "status": response.status,
                    "request_reference": request_id,
                },
                transient=response.status in {408, 429, 500, 502, 503, 504},
            )
        if response.status == 204 or not response.body:
            return response.status, None
        return response.status, response.json()

    def _paged(self, path: str, params: Mapping[str, Any] | None = None) -> list[Mapping[str, Any]]:
        results: list[Mapping[str, Any]] = []
        query = dict(params or {})
        seen_cursors: set[str] = set()
        while True:
            _, payload = self._request("GET", path, params=query)
            page = _items(payload, path)
            results.extend(page)
            has_more = payload.get("has_more") if isinstance(payload, Mapping) else None
            if has_more is False:
                return results
            if has_more is not True:
                raise RuntimeFailure(
                    "invalid_agent_insights_response",
                    f"{path} response did not declare has_more.",
                )
            raw_cursor = payload.get("last_id") if isinstance(payload, Mapping) else None
            cursor = str(raw_cursor or "")
            if not cursor or cursor in seen_cursors:
                raise RuntimeFailure(
                    "invalid_agent_insights_response",
                    f"{path} pagination cursor is missing or repeated.",
                )
            seen_cursors.add(cursor)
            query["after"] = cursor

    def probe(self) -> None:
        self._request("GET", "/agent_insight_monitors", params={"limit": 1})

    def find_agent(self, agent_name: str) -> Mapping[str, Any]:
        matches = [
            item
            for item in self._paged("/agents", {"limit": 100})
            if str(item.get("name") or "") == agent_name
        ]
        if len(matches) != 1:
            raise RuntimeFailure(
                "agent_lookup_failed",
                "Exact agent lookup did not return one agent.",
                {"match_count": len(matches)},
            )
        return matches[0]

    def list_monitors(self) -> list[Mapping[str, Any]]:
        return self._paged("/agent_insight_monitors", {"limit": 100})

    def find_monitor(self, agent_name: str) -> Mapping[str, Any] | None:
        matches = [
            item for item in self.list_monitors() if str(item.get("agent_name") or "") == agent_name
        ]
        if len(matches) > 1:
            raise RuntimeFailure("ambiguous_monitor", "More than one exact monitor exists for the agent.")
        return matches[0] if matches else None

    def create_monitor(
        self,
        *,
        agent_name: str,
        model_deployment_name: str,
        expires_on: date,
        run_interval_hours: float = 24,
    ) -> Mapping[str, Any]:
        if self._ownership is None:
            raise RuntimeFailure(
                "monitor_receipt_unavailable",
                "Monitor creation requires a public-safe ownership registry.",
            )
        self.find_agent(agent_name)
        _, payload = self._request(
            "POST",
            "/agent_insight_monitors",
            json_body={
                "agent_name": agent_name,
                "enabled": False,
                "run_interval_hours": run_interval_hours,
                "model_deployment_name": model_deployment_name,
            },
            expected={200, 201},
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise RuntimeFailure("invalid_monitor", "Created monitor response was invalid.")
        monitor_id = str(payload["id"])
        try:
            self._ownership.record(
                agent_name=agent_name,
                monitor_id=monitor_id,
                model_deployment_name=model_deployment_name,
                expires_on=expires_on,
            )
        except RuntimeFailure as receipt_error:
            try:
                self._request(
                    "DELETE",
                    f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}",
                    expected={200, 204},
                )
            except RuntimeFailure:
                receipt_error.details["monitor_rollback_failed"] = True
            raise receipt_error
        return payload

    def get_or_create_monitor(
        self,
        *,
        agent_name: str,
        model_deployment_name: str,
        expires_on: date,
    ) -> tuple[Mapping[str, Any], bool]:
        existing = self.find_monitor(agent_name)
        if existing is not None:
            if self._ownership is None:
                raise RuntimeFailure(
                    "monitor_receipt_unavailable",
                    "Monitor reuse requires a public-safe ownership registry.",
                )
            self._ownership.require(
                agent_name=agent_name,
                monitor_id=str(existing.get("id") or ""),
                model_deployment_name=model_deployment_name,
            )
            return existing, False
        return (
            self.create_monitor(
                agent_name=agent_name,
                model_deployment_name=model_deployment_name,
                expires_on=expires_on,
            ),
            True,
        )

    def update_monitor(self, monitor_id: str, changes: Mapping[str, Any]) -> Mapping[str, Any]:
        _, payload = self._request(
            "PATCH",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}",
            json_body=changes,
        )
        if not isinstance(payload, Mapping):
            raise RuntimeFailure("invalid_monitor", "Updated monitor response was invalid.")
        return payload

    def get_monitor(self, monitor_id: str) -> Mapping[str, Any]:
        _, payload = self._request(
            "GET",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}",
        )
        if not isinstance(payload, Mapping):
            raise RuntimeFailure("invalid_monitor", "Monitor response was invalid.")
        return payload

    def reset_monitor(self, monitor_id: str, agent_name: str) -> Mapping[str, Any]:
        if self._ownership is None:
            raise RuntimeFailure("monitor_receipt_unavailable", "Monitor reset requires ownership receipts.")
        self._ownership.require(agent_name=agent_name, monitor_id=monitor_id)
        _, payload = self._request(
            "POST",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}:reset",
            expected={200, 202},
        )
        if not isinstance(payload, Mapping):
            raise RuntimeFailure("invalid_monitor", "Monitor reset response was invalid.")
        return payload

    def delete_monitor(self, monitor_id: str, agent_name: str) -> None:
        if self._ownership is None:
            raise RuntimeFailure("monitor_receipt_unavailable", "Monitor deletion requires ownership receipts.")
        self._ownership.require(agent_name=agent_name, monitor_id=monitor_id)
        self._request(
            "DELETE",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}",
            expected={200, 204},
        )
        self._ownership.remove(agent_name=agent_name, monitor_id=monitor_id)

    def create_run(
        self,
        monitor_id: str,
        *,
        lookback_hours: int,
    ) -> Mapping[str, Any]:
        if lookback_hours <= 0:
            raise RuntimeFailure("invalid_run_window", "Positive lookback_hours is required.")
        _, payload = self._request(
            "POST",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}/runs",
            json_body={"lookback_hours": lookback_hours},
            expected={200, 201, 202},
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise RuntimeFailure("invalid_insights_run", "Run create response was invalid.")
        return payload

    def list_runs(self, monitor_id: str) -> list[Mapping[str, Any]]:
        return self._paged(
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}/runs",
            {"limit": 100, "order": "desc"},
        )

    def get_run(self, monitor_id: str, run_id: str) -> Mapping[str, Any]:
        _, payload = self._request(
            "GET",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}/runs/"
            f"{_segment(run_id, 'run ID')}",
        )
        if not isinstance(payload, Mapping):
            raise RuntimeFailure("invalid_insights_run", "Run response was invalid.")
        return payload

    def wait_run(
        self,
        monitor_id: str,
        run_id: str,
        *,
        timeout_seconds: float = 21600,
        poll_seconds: float = 30,
    ) -> Mapping[str, Any]:
        deadline = self._monotonic() + timeout_seconds
        while True:
            payload = self.get_run(monitor_id, run_id)
            payload_id = str(payload.get("id") or "")
            payload_monitor = str(payload.get("monitor_id") or payload.get("monitorId") or "")
            if payload_id != run_id:
                raise RuntimeFailure("run_identity_mismatch", "Run result ID did not match the requested run.")
            if payload_monitor and payload_monitor != monitor_id:
                raise RuntimeFailure("run_identity_mismatch", "Run result monitor did not match the request.")
            status = str(payload.get("status") or "").casefold()
            if status in _TERMINAL:
                if status != "succeeded":
                    service_error = payload.get("error")
                    code = str(service_error.get("code") or "") if isinstance(service_error, Mapping) else ""
                    raise RuntimeFailure(
                        "insights_run_failed",
                        f"Agent Insights run reached terminal state {status}.",
                        {"service_error_code": code},
                    )
                return payload
            if self._monotonic() >= deadline:
                raise RuntimeFailure("insights_run_timeout", "Agent Insights run polling timed out.")
            self._sleep(poll_seconds)

    def cancel_run(self, monitor_id: str, run_id: str) -> None:
        self._request(
            "POST",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}/runs/"
            f"{_segment(run_id, 'run ID')}:cancel",
            expected={200, 202, 204},
        )

    def list_insights(
        self,
        monitor_id: str,
    ) -> list[Mapping[str, Any]]:
        path = f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}/insights"
        summaries = self._paged(path, {"limit": 100, "order": "desc"})
        details: list[Mapping[str, Any]] = []
        for summary in summaries:
            insight_id = str(summary.get("id") or "")
            if not insight_id:
                raise RuntimeFailure("invalid_insight", "Insight summary did not contain an ID.")
            _, payload = self._request(
                "GET",
                f"{path}/{_segment(insight_id, 'insight ID')}",
                params={"include_details": "true"},
            )
            if not isinstance(payload, Mapping):
                raise RuntimeFailure("invalid_insight", "Insight detail response was invalid.")
            details.append(payload)
        return details

    def capture_insight_checkpoint(self, monitor_id: str) -> InsightCheckpoint:
        revisions: dict[str, str] = {}
        details: dict[str, Mapping[str, Any]] = {}
        for insight in self.list_insights(monitor_id):
            insight_id = str(insight.get("id") or "")
            revision = str(_field(insight, "revision", "etag", "updated_at", "updatedAt") or "")
            if not insight_id or not revision:
                raise RuntimeFailure(
                    "insight_checkpoint_unavailable",
                    "Existing insight lacks revision evidence required for run scoping.",
                )
            revisions[insight_id] = revision
            details[insight_id] = dict(insight)
        return InsightCheckpoint(datetime.now(UTC), revisions, details)

    @staticmethod
    def validate_run_window(
        run: Mapping[str, Any],
        expected_start: datetime,
        expected_end: datetime,
    ) -> tuple[datetime, datetime]:
        actual_start = _timestamp(_field(run, "start_time", "startTime", "window_start"), "run start")
        actual_end = _timestamp(_field(run, "end_time", "endTime", "window_end"), "run end")
        if expected_start.tzinfo is None or expected_end.tzinfo is None:
            raise RuntimeFailure("invalid_run_window", "Expected run window must be timezone-aware.")
        if (
            actual_start != expected_start.astimezone(UTC)
            or actual_end != expected_end.astimezone(UTC)
            or actual_start >= actual_end
        ):
            raise RuntimeFailure(
                "run_window_mismatch",
                "Agent Insights returned a different analysis window.",
            )
        return actual_start, actual_end

    @staticmethod
    def scope_insights(
        insights: Sequence[Mapping[str, Any]],
        checkpoint: InsightCheckpoint,
        run_start: datetime,
        run_end: datetime,
    ) -> list[Mapping[str, Any]]:
        selected: list[Mapping[str, Any]] = []
        for insight in insights:
            insight_id = str(insight.get("id") or "")
            revision = str(_field(insight, "revision", "etag", "updated_at", "updatedAt") or "")
            if not insight_id or not revision:
                raise RuntimeFailure(
                    "insight_scope_unproven",
                    "Insight lacks ID or revision evidence required for run scoping.",
                )
            if checkpoint.revisions.get(insight_id) == revision:
                continue
            observed = _timestamp(
                _field(insight, "updated_at", "updatedAt", "created_at", "createdAt"),
                "insight evidence",
            )
            if not (run_start <= observed < run_end) or observed < checkpoint.captured_at:
                raise RuntimeFailure(
                    "insight_scope_unproven",
                    "Changed insight cannot be proven to belong to the completed run.",
                )
            selected.append(insight)
        if len(selected) > 100:
            raise RuntimeFailure(
                "insight_limit_exceeded",
                "Agent Insights exceeded the structural limit of 100 run-scoped insights.",
                {"insight_count": len(selected)},
            )
        return selected

    def collect_run(
        self,
        monitor_id: str,
        run_id: str,
        *,
        checkpoint: InsightCheckpoint,
        expected_start: datetime,
        expected_end: datetime,
        timeout_seconds: float = 21600,
    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        run = self.wait_run(monitor_id, run_id, timeout_seconds=timeout_seconds)
        run_start, run_end = self.validate_run_window(run, expected_start, expected_end)
        insights = self.scope_insights(
            self.list_insights(monitor_id),
            checkpoint,
            run_start,
            run_end,
        )
        return run, insights

    def cleanup_owned_monitors(
        self,
        *,
        now: date | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        selected: list[str] = []
        failures = 0
        today = now or datetime.now(UTC).date()
        for monitor in self.list_monitors():
            monitor_id = str(monitor.get("id") or "")
            agent_name = str(monitor.get("agent_name") or "")
            if not monitor_id or not agent_name or self._ownership is None:
                continue
            try:
                ownership = self._ownership.require(agent_name=agent_name, monitor_id=monitor_id)
                expired = date.fromisoformat(ownership.expires_on) < today
            except (RuntimeFailure, ValueError):
                continue
            if not expired:
                continue
            if not dry_run:
                try:
                    self.delete_monitor(monitor_id, agent_name)
                except RuntimeFailure:
                    failures += 1
                    continue
            selected.append(monitor_id)
        if failures:
            raise RuntimeFailure(
                "cleanup_partial_failure",
                "One or more owned monitors could not be deleted; other eligible monitors were processed.",
                {"deleted_count": len(selected), "failure_count": failures},
            )
        return selected
