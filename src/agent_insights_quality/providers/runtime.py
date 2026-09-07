from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from agent_insights_quality.contracts import (
    Deployment,
    Environment,
    Invocation,
    JsonObject,
    QueryResult,
    Step,
    Target,
)
from agent_insights_quality.errors import QualityError
from agent_insights_quality.invocation_context import invocation_headers
from agent_insights_quality.providers.artifacts import ImageBuilder
from agent_insights_quality.providers.callbacks import safe_persist
from agent_insights_quality.providers.deployment import DeploymentClient
from agent_insights_quality.providers.insights import InsightsClient
from agent_insights_quality.providers.telemetry import AzureLogsReader, LogsReader
from agent_insights_quality.providers.transport import (
    AzureHttpTransport,
    JsonClient,
    Transport,
    check_status,
    segment,
)


class AzureRuntime:
    """CloudPort with checkpoint callbacks; ordinary POSTs are never retried here."""

    def __init__(
        self,
        environment: Environment,
        *,
        transport: Transport | None = None,
        logs: LogsReader | None = None,
        images: ImageBuilder | None = None,
        hosted_environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if environment.profile not in {"staging", "daily"}:
            raise QualityError("environment_profile_invalid")
        self.environment = environment
        transport = transport if transport is not None else AzureHttpTransport()
        self._client = JsonClient(environment.project_endpoint, transport, sleep=sleep)
        self._deployments = DeploymentClient(
            environment,
            self._client,
            images=images,
            hosted_environment=hosted_environment,
        )
        self._insights = InsightsClient(
            environment,
            JsonClient(
                environment.insights_endpoint or environment.project_endpoint,
                transport,
                sleep=sleep,
            ),
        )
        self._logs = (
            logs
            if logs is not None
            else AzureLogsReader(environment.application_insights_resource_id)
        )
        self._clock = clock

    async def ensure_deployment(
        self,
        target: Target,
        source_revision: str,
        existing: Deployment | None,
        persist: Callable[[Deployment], None],
        *,
        resume: bool = False,
    ) -> Deployment:
        return await self._deployments.ensure_deployment(
            target, source_revision, existing, persist, resume=resume,
        )

    async def activate(self, deployment: Deployment) -> None:
        await self._deployments.activate(deployment)

    async def create_session(
        self, deployment: Deployment, request_id: str, persist: Callable[[str], None]
    ) -> str:
        persist = safe_persist(persist)
        if deployment.agent_type == "prompt":
            raise QualityError("prompt_session_not_supported", request_accepted=False)
        segment(deployment.provider_version)
        segment(request_id)
        response = await self._client.request(
            "POST",
            "/agents/" + segment(deployment.agent_name) + "/endpoint/sessions",
            {
                "version_indicator": {
                    "type": "version_ref",
                    "agent_version": deployment.provider_version,
                }
            },
            hosted=True,
            headers={"x-ms-client-request-id": request_id},
        )
        check_status(response, {200, 201, 202})
        value = response.object()
        identity = (
            value.get("agent_session_id") or value.get("session_id") or value.get("id")
        )
        if not isinstance(identity, str) or not identity:
            raise QualityError("session_identity_missing", request_accepted=True)
        persist(identity)
        indicator = value.get("version_indicator")
        if (
            not isinstance(indicator, dict)
            or indicator.get("type") != "version_ref"
            or str(indicator.get("agent_version") or "") != deployment.provider_version
        ):
            raise QualityError("session_version_mismatch", request_accepted=True)
        if response.status == 202:
            raise QualityError("session_pending", request_accepted=True)
        return identity

    async def invoke(
        self,
        deployment: Deployment,
        step: Step,
        *,
        request_id: str,
        session_id: str | None,
        previous_response_id: str | None,
        persist: Callable[[Invocation], None],
    ) -> Invocation:
        persist = safe_persist(persist)
        segment(deployment.provider_version)
        segment(request_id)
        prompt = deployment.agent_type == "prompt"
        if "input" not in step.body:
            raise QualityError("invocation_input_missing", request_accepted=False)
        reserved = {
            "agent_reference",
            "agent_session_id",
            "previous_response_id",
            "conversation",
            "store",
        }
        if reserved.intersection(step.body):
            raise QualityError("invocation_reserved_field", request_accepted=False)
        if prompt:
            if session_id is not None:
                raise QualityError(
                    "prompt_session_not_supported", request_accepted=False
                )
            body: dict[str, Any] = {
                **step.body,
                "agent_reference": {
                    "type": "agent_reference",
                    "name": deployment.agent_name,
                    "version": deployment.provider_version,
                },
                "store": True,
            }
            if previous_response_id is not None:
                segment(previous_response_id)
                body["previous_response_id"] = previous_response_id
            path = "/openai/v1/responses"
        else:
            if not session_id or previous_response_id is not None:
                raise QualityError("hosted_session_required", request_accepted=False)
            unsupported = set(step.body) - {"input", "metadata"}
            if unsupported:
                raise QualityError(
                    "hosted_request_field_unsupported", request_accepted=False
                )
            body = {**step.body, "agent_session_id": session_id, "store": False}
            path = (
                "/agents/"
                + segment(deployment.agent_name)
                + "/endpoint/protocols/openai/responses"
            )
        started = self._clock().isoformat()
        persist(Invocation(request_id, None, session_id, started, "", "submitting"))
        try:
            response = await self._client.request(
                "POST",
                path,
                body,
                hosted=not prompt,
                headers={"x-ms-client-request-id": request_id, **invocation_headers(request_id)},
                api_version=None if prompt else "v1",
            )
        except QualityError as error:
            persist(
                Invocation(
                    request_id,
                    None,
                    session_id,
                    started,
                    self._clock().isoformat(),
                    "failed" if error.request_accepted is False else "unknown",
                    error_code=error.code,
                    http_status=error.status,
                )
            )
            raise
        try:
            value = response.object()
        except QualityError as error:
            persist(
                Invocation(
                    request_id,
                    None,
                    session_id,
                    started,
                    self._clock().isoformat(),
                    "unknown",
                    response={
                        "raw_body": response.body.decode("utf-8", errors="replace")
                    },
                    http_status=response.status,
                    error_code=error.code,
                )
            )
            raise
        response_id = value.get("id")
        response_id = (
            response_id if isinstance(response_id, str) and response_id else None
        )
        state = str(value.get("status") or "")
        error_code = None
        if not 200 <= response.status < 300:
            rejected = 400 <= response.status < 500 and response.status != 408
            state, error_code = (
                "failed" if rejected else "unknown",
                "provider_http_error",
            )
        elif value.get("error") or state in {"failed", "cancelled", "canceled"}:
            state, error_code = "failed", "invocation_response_failed"
        elif state == "incomplete":
            error_code = "invocation_response_incomplete"
        elif state in {"queued", "in_progress"} or response.status == 202:
            state, error_code = "incomplete", "invocation_response_pending"
        elif response_id is None:
            state, error_code = "unknown", "invocation_response_identity_missing"
        elif state != "completed":
            state, error_code = "unknown", "invocation_response_status_unknown"
        result = Invocation(
            request_id,
            response_id,
            session_id,
            started,
            self._clock().isoformat(),
            state,
            value,
            response.status,
            error_code,
        )
        persist(result)
        check_status(response, {200, 201, 202})
        if state == "unknown":
            raise QualityError(
                error_code, request_accepted=True, status=response.status
            )
        return result

    async def query(self, query: str, *, start: str, end: str) -> QueryResult:
        return await self._logs.query(query, start=start, end=end)

    async def ensure_monitor(self, agent_name: str) -> str:
        return await self._insights.ensure_monitor(agent_name)

    async def reset_monitor(self, monitor_id: str) -> None:
        await self._insights.reset_monitor(monitor_id)

    async def start_insights(
        self,
        monitor_id: str,
        lookback_hours: float,
        operation_id: str,
        persist: Callable[[JsonObject], None],
    ) -> JsonObject:
        return await self._insights.start_insights(
            monitor_id, lookback_hours, operation_id, persist
        )

    async def get_insights_run(self, monitor_id: str, run_id: str) -> JsonObject:
        return await self._insights.get_insights_run(monitor_id, run_id)

    async def list_insights(self, monitor_id: str) -> tuple[JsonObject, ...]:
        return await self._insights.list_insights(monitor_id)
