from __future__ import annotations

import copy
import math
from collections.abc import Callable

from agent_insights_quality.contracts import Environment, JsonObject
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.callbacks import safe_persist
from agent_insights_quality.providers.transport import JsonClient, check_status, segment


class InsightsClient:
    def __init__(self, environment: Environment, client: JsonClient) -> None:
        self.environment = environment
        self.client = client
        self._submissions: dict[str, tuple[str, float]] = {}

    def _daily(self) -> None:
        if self.environment.profile != "daily":
            raise QualityError("insights_daily_only", request_accepted=False)

    async def ensure_monitor(self, agent_name: str) -> str:
        self._daily()
        segment(agent_name)
        values = await self.client.pages("/agent_insight_monitors?limit=100")
        matches = [value for value in values if value.get("agent_name") == agent_name]
        if len(matches) > 1:
            raise QualityError("insights_monitor_ambiguous")
        value = (
            matches[0]
            if matches
            else await self.client.object(
                "POST",
                "/agent_insight_monitors",
                {
                    "agent_name": agent_name,
                    "enabled": False,
                    "run_interval_hours": 24,
                    "model_deployment_name": "terra-insight-generation",
                },
            )
        )
        identity = value.get("id")
        if not isinstance(identity, str) or not identity:
            raise QualityError(
                "insights_monitor_identity_missing", request_accepted=True
            )
        return identity

    async def reset_monitor(self, monitor_id: str) -> None:
        self._daily()
        response = await self.client.request(
            "POST", "/agent_insight_monitors/" + segment(monitor_id) + ":reset"
        )
        check_status(response, {200, 202, 204})
        if response.status == 202:
            raise QualityError("insights_reset_pending", request_accepted=True)

    async def start_insights(
        self,
        monitor_id: str,
        lookback_hours: float,
        operation_id: str,
        persist: Callable[[JsonObject], None],
    ) -> JsonObject:
        persist = safe_persist(persist)
        self._daily()
        if (
            isinstance(lookback_hours, bool)
            or not isinstance(lookback_hours, (int, float))
            or not math.isfinite(lookback_hours)
            or lookback_hours <= 0
        ):
            raise QualityError("insights_lookback_invalid", request_accepted=False)
        segment(operation_id)
        if any(ord(char) < 33 or ord(char) > 126 for char in operation_id):
            raise QualityError("insights_operation_id_invalid", request_accepted=False)
        binding = (monitor_id, lookback_hours)
        if (
            operation_id in self._submissions
            and self._submissions[operation_id] != binding
        ):
            raise QualityError(
                "insights_operation_body_changed", request_accepted=False
            )
        self._submissions[operation_id] = binding
        body = {"lookback_hours": lookback_hours}
        checkpoint = {
            "operation_id": operation_id,
            "monitor_id": monitor_id,
            "request_body": body,
            "submission_state": "submitting",
        }
        persist(copy.deepcopy(checkpoint))
        try:
            response = await self.client.request(
                "POST",
                "/agent_insight_monitors/" + segment(monitor_id) + "/runs",
                body,
                headers={"Operation-Id": operation_id},
            )
            check_status(response, {200, 201, 202}, idempotent=True)
        except QualityError as error:
            persist(
                {
                    **checkpoint,
                    "submission_state": "rejected"
                    if error.request_accepted is False
                    else "unknown",
                    "error_code": error.code,
                    "http_status": error.status,
                }
            )
            raise QualityError(
                error.code,
                retryable=error.retryable or error.request_accepted is None,
                request_accepted=error.request_accepted,
                status=error.status,
            ) from None
        try:
            run = response.object()
        except QualityError:
            persist(
                {
                    **checkpoint,
                    "submission_state": "accepted",
                    "http_status": response.status,
                }
            )
            raise
        polling = response.header("Operation-Location") or response.header("Location")
        saved = {
            **run,
            **checkpoint,
            "submission_state": "accepted",
            "provider_response": run,
            "http_status": response.status,
            "polling_url": polling,
        }
        persist(copy.deepcopy(saved))
        if not isinstance(run.get("id"), str) or not run["id"]:
            raise QualityError(
                "insights_run_identity_pending", request_accepted=True, retryable=True
            )
        return saved

    async def get_insights_run(self, monitor_id: str, run_id: str) -> JsonObject:
        self._daily()
        return await self.client.object(
            "GET",
            "/agent_insight_monitors/"
            + segment(monitor_id)
            + "/runs/"
            + segment(run_id),
        )

    async def list_insights(self, monitor_id: str) -> tuple[JsonObject, ...]:
        self._daily()
        return tuple(
            await self.client.pages(
                "/agent_insight_monitors/"
                + segment(monitor_id)
                + "/insights?include_details=true&limit=100"
            )
        )
