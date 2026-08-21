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
            with urllib.request.urlopen(request, timeout=timeout) as response:
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


class AgentInsightsClient:
    def __init__(
        self,
        project_endpoint: str,
        credential: TokenCredential,
        *,
        transport: Transport | None = None,
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
        absolute_url: str | None = None,
    ) -> tuple[int, Any]:
        url = absolute_url or self._url(path, params)
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
        next_url: str | None = None
        visited: set[str] = set()
        first = True
        while first or next_url:
            first = False
            _, payload = self._request(
                "GET",
                path,
                params=params if next_url is None else None,
                absolute_url=next_url,
            )
            results.extend(_items(payload, path))
            raw_next = payload.get("next_link") or payload.get("nextLink") if isinstance(payload, Mapping) else None
            next_url = urllib.parse.urljoin(self._base + "/", str(raw_next)) if raw_next else None
            if next_url and next_url in visited:
                raise RuntimeFailure("invalid_pagination_link", "Pagination link formed a cycle.")
            if next_url:
                visited.add(next_url)
        return results

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
        owner_reference: str,
        expires_on: date | None = None,
        run_interval_hours: float = 24,
    ) -> Mapping[str, Any]:
        self.find_agent(agent_name)
        _, payload = self._request(
            "POST",
            "/agent_insight_monitors",
            json_body={
                "agent_name": agent_name,
                "enabled": False,
                "run_interval_hours": run_interval_hours,
                "model_deployment_name": model_deployment_name,
                "metadata": {
                    "purpose": "agent-insights-quality",
                    "owner_reference": owner_reference,
                    "expires_on": expires_on.isoformat() if expires_on else "",
                },
            },
            expected={200, 201},
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise RuntimeFailure("invalid_monitor", "Created monitor response was invalid.")
        return payload

    def get_or_create_monitor(
        self,
        *,
        agent_name: str,
        model_deployment_name: str,
        owner_reference: str,
        expires_on: date | None = None,
    ) -> tuple[Mapping[str, Any], bool]:
        existing = self.find_monitor(agent_name)
        if existing is not None:
            metadata = existing.get("metadata")
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("purpose") != "agent-insights-quality"
                or metadata.get("owner_reference") != owner_reference
                or str(existing.get("model_deployment_name") or "") != model_deployment_name
                or (
                    expires_on is not None
                    and metadata.get("expires_on") != expires_on.isoformat()
                )
            ):
                raise RuntimeFailure(
                    "ownership_mismatch",
                    "Existing exact monitor does not match runtime ownership and configuration.",
                )
            return existing, False
        return (
            self.create_monitor(
                agent_name=agent_name,
                model_deployment_name=model_deployment_name,
                owner_reference=owner_reference,
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

    def reset_monitor(self, monitor_id: str, owner_reference: str) -> Mapping[str, Any]:
        monitor = self.get_monitor(monitor_id)
        metadata = monitor.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("owner_reference") != owner_reference:
            raise RuntimeFailure("ownership_mismatch", "Monitor reset requires exact ownership.")
        return self.update_monitor(monitor_id, {"enabled": False})

    def delete_monitor(self, monitor_id: str) -> None:
        self._request(
            "DELETE",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}",
            expected={200, 204},
        )

    def create_run(
        self,
        monitor_id: str,
        *,
        lookback_hours: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Mapping[str, Any]:
        has_window = start is not None or end is not None
        if has_window:
            if start is None or end is None or start.tzinfo is None or end.tzinfo is None or start >= end:
                raise RuntimeFailure("invalid_run_window", "Run window must be a valid half-open UTC interval.")
            if lookback_hours is not None:
                raise RuntimeFailure("invalid_run_window", "Specify either lookback or an exact window.")
            body: dict[str, Any] = {
                "start_time": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "end_time": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
        else:
            if lookback_hours is None or lookback_hours <= 0:
                raise RuntimeFailure("invalid_run_window", "Positive lookback_hours is required.")
            body = {"lookback_hours": lookback_hours}
        _, payload = self._request(
            "POST",
            f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}/runs",
            json_body=body,
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
            f"{_segment(run_id, 'run ID')}/cancel",
            expected={200, 202, 204},
        )

    def list_insights(
        self,
        monitor_id: str,
        run_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        path = f"/agent_insight_monitors/{_segment(monitor_id, 'monitor ID')}/insights"
        summaries = self._paged(path, {"limit": 100, "order": "desc", "run_id": run_id})
        if len(summaries) > 5:
            raise RuntimeFailure(
                "insight_limit_exceeded",
                "Agent Insights returned more than five insights.",
                {"insight_count": len(summaries)},
            )
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
            detail_run = str(payload.get("run_id") or payload.get("runId") or "")
            if run_id and detail_run and detail_run != run_id:
                raise RuntimeFailure(
                    "run_identity_mismatch",
                    "Insight detail belonged to a different run.",
                )
            details.append(payload)
        return details

    def collect_run(
        self,
        monitor_id: str,
        run_id: str,
        *,
        timeout_seconds: float = 21600,
    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        run = self.wait_run(monitor_id, run_id, timeout_seconds=timeout_seconds)
        insights = self.list_insights(monitor_id, run_id)
        return run, insights

    def cleanup_owned_monitors(
        self,
        owner_reference: str,
        *,
        now: date | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        selected: list[str] = []
        today = now or datetime.now(UTC).date()
        for monitor in self.list_monitors():
            metadata = monitor.get("metadata")
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("purpose") != "agent-insights-quality"
                or metadata.get("owner_reference") != owner_reference
            ):
                continue
            try:
                expired = date.fromisoformat(str(metadata.get("expires_on") or "")) < today
            except ValueError:
                continue
            if not expired:
                continue
            monitor_id = str(monitor.get("id") or "")
            if not monitor_id:
                continue
            selected.append(monitor_id)
            if not dry_run:
                self.delete_monitor(monitor_id)
        return selected
