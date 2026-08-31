from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_insights_quality.models import (
    InsightEvidence,
    InsightRunCheckpoint,
    InsightRunEvidence,
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    linked_operations_match_scope,
)
from agent_insights_quality.automation_policy import (
    TRACE_ASSERTION_DEADLINE_SECONDS,
    TRACE_ASSERTION_POLL_SECONDS,
    TRAFFIC_UNCERTAINTY_SECONDS,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.runtime_state import TrafficLedger
from agent_insights_quality.util import (
    ContractError,
    InsightWindowExpiredError,
    TraceAssertionActivationError,
    json_values_equal,
)
from agent_insights_quality.azure_cli import azure_cli

try:
    from azure.core.exceptions import (
        HttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    )

    _TELEMETRY_HTTP_ERRORS = (HttpResponseError,)
    _TELEMETRY_TRANSIENT_ERRORS = (
        HttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    )
except ImportError:
    _TELEMETRY_HTTP_ERRORS = ()
    _TELEMETRY_TRANSIENT_ERRORS = ()

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_RESPONSE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$", re.ASCII)
_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
_LOGS_SCOPE = "https://api.loganalytics.io/.default"
_TRANSIENT_HTTP = {408, 424, 429, 500, 502, 503, 504}
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 300
_HOSTED_RESPONSE_TIMEOUT_SECONDS = 600
_PROMPT_RESPONSE_PROPAGATION_RETRY_DELAYS = (1, 2)
_TRACE_ASSERTION_PROGRESS_SECONDS = 60
_AUTH_PROGRESS = ProgressReporter("aiq-auth")


class RemoteOperationError(ContractError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int | None,
        request_accepted: bool | None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_accepted = request_accepted


class _RuntimeTokenCredential:
    def __init__(self, runtime: LiveRuntime) -> None:
        self._runtime = runtime

    def get_token(self, *scopes: str, **_kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken

        scope = scopes[0] if scopes else _LOGS_SCOPE
        return AccessToken(
            self._runtime._token_provider(scope),
            int(time.time()) + 10 * 60,
        )


class LiveRuntime:
    """Endpoint-only traffic with read-only telemetry access."""

    def __init__(
        self,
        profile: RuntimeProfile,
        *,
        token_provider: Callable[[str], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = profile
        self._raw_token_provider = token_provider or _azure_cli_token
        self._sleep = sleep
        self._utcnow = utcnow
        self._monotonic = monotonic
        self._token_lock = threading.Lock()
        self._token_cache: dict[str, tuple[float, str]] = {}
        self._rate_limit_feedback = threading.local()
        self._telemetry_query_lock = threading.Lock()
        self._logs_client_instance: Any | None = None
        self._progress = ProgressReporter("aiq", monotonic=monotonic)
        self._traffic_ledger = TrafficLedger(profile.name)

    def report_progress(self, message: str) -> None:
        self._progress.emit(message)

    def rate_limit_feedback(self) -> dict[str, int | float | None]:
        value = getattr(self._rate_limit_feedback, "value", None)
        return (
            dict(value)
            if isinstance(value, dict)
            else {
                "remaining_requests": None,
                "remaining_tokens": None,
                "retry_after_seconds": None,
            }
        )

    def _token_provider(self, scope: str) -> str:
        with self._token_lock:
            cached = self._token_cache.get(scope)
            now = time.monotonic()
            if cached is not None and now - cached[0] < 10 * 60:
                return cached[1]
            token = self._raw_token_provider(scope)
            self._token_cache[scope] = (now, token)
            return token

    def _invalidate_token(self, scope: str) -> None:
        with self._token_lock:
            self._token_cache.pop(scope, None)

    def _logs_client(self) -> Any:
        if self._logs_client_instance is None:
            from azure.monitor.query import LogsQueryClient

            self._logs_client_instance = LogsQueryClient(
                _RuntimeTokenCredential(self)
            )
        return self._logs_client_instance

    def _query_resource(
        self,
        client: Any,
        query: str,
        *,
        timespan: Any,
    ) -> Any:
        for attempt in range(4):
            try:
                with self._telemetry_query_lock:
                    with self._progress.heartbeat("Azure Monitor query"):
                        return client.query_resource(
                            self._profile.application_insights_resource_id,
                            query,
                            timespan=timespan,
                        )
            except _TELEMETRY_TRANSIENT_ERRORS as error:
                if (
                    isinstance(error, _TELEMETRY_HTTP_ERRORS)
                    and error.status_code not in _TRANSIENT_HTTP
                ):
                    raise
                if attempt == 3:
                    raise
                delay = 2**attempt
                self.report_progress(
                    f"telemetry query failed transiently; retrying in {delay}s "
                    f"({attempt + 2}/4)"
                )
                self._sleep(delay)
        raise ContractError("Telemetry query retry loop did not execute")

    def assert_telemetry_read_access(self) -> None:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Telemetry preflight requires installation with ".[azure]"'
            ) from error
        result = self._query_resource(
            self._logs_client(),
            "print readiness=1",
            timespan=timedelta(minutes=1),
        )
        if result.status != LogsQueryStatus.SUCCESS:
            raise ContractError("Read-only telemetry preflight failed")

    def reset_monitor(self, agent_name: str, monitor_id: str) -> None:
        del agent_name
        self._json_request(
            "POST",
            self._insights_url(
                f"/agent_insight_monitors/{urllib.parse.quote(monitor_id, safe='')}:reset"
            ),
            expected={200, 202, 204},
            retry_statuses={409, *_TRANSIENT_HTTP},
        )

    def wait_for_clean_window(
        self,
        agent_name: str,
        lookback_hours: float,
        *,
        poll_seconds: int,
        ingestion_margin_seconds: int,
        max_wait_seconds: int,
    ) -> None:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Clean-window preflight requires installation with ".[azure]"'
            ) from error
        lookback_seconds = int(round(lookback_hours * 3600))
        query_seconds = lookback_seconds + ingestion_margin_seconds
        query = f"""
union traces, dependencies, requests
| where timestamp >= ago({query_seconds}s)
| extend operation_name = tostring(customDimensions["gen_ai.operation.name"])
| extend observed_agent = tostring(customDimensions["gen_ai.agent.name"])
| where operation_name == "invoke_agent" and observed_agent == "{agent_name}"
| summarize latest=max(timestamp), operation_count=dcount(operation_Id)
"""
        deadline = self._monotonic() + max_wait_seconds
        next_progress = self._monotonic()
        while self._monotonic() < deadline:
            now = self._utcnow().astimezone(UTC)
            ledger_ready = self._traffic_ledger.clean_after(
                agent_name,
                lookback_seconds=lookback_seconds,
                margin_seconds=ingestion_margin_seconds,
            )
            result = self._query_resource(
                self._logs_client(),
                query,
                timespan=timedelta(seconds=query_seconds),
            )
            if result.status != LogsQueryStatus.SUCCESS or not result.tables:
                raise ContractError("Clean-window telemetry query failed")
            latest = None
            count = 0
            if result.tables[0].rows:
                latest = result.tables[0].rows[0][0]
                count = int(result.tables[0].rows[0][1] or 0)
            telemetry_ready = (
                latest.astimezone(UTC) + timedelta(seconds=query_seconds)
                if count and isinstance(latest, datetime)
                else None
            )
            ready_at = max(
                value for value in (ledger_ready, telemetry_ready, now) if value is not None
            )
            if ready_at <= now:
                return
            if self._monotonic() >= next_progress:
                remaining = max(1, int((ready_at - now).total_seconds()))
                self.report_progress(
                    f"{agent_name}: waiting {remaining}s for clean telemetry window"
                )
                next_progress = self._monotonic() + 30
            self._sleep(min(poll_seconds, max(1, (ready_at - now).total_seconds())))
        raise ContractError("Clean telemetry window did not become ready before deadline")

    def invoke_version(
        self,
        *,
        agent_name: str,
        agent_type: str,
        foundry_version: str,
        traffic_path: Path,
        seed: int,
    ) -> InvocationEvidence:
        payload = json.loads(traffic_path.read_text(encoding="utf-8"))
        requests = payload if isinstance(payload, list) else payload.get("requests")
        if not isinstance(requests, list) or len(requests) < 1:
            raise ContractError(f"{traffic_path} has no traffic requests")
        normalized = [
            {**_normalize_fixture(item), "_index": index}
            for index, item in enumerate(requests)
        ]
        groups: dict[str, list[dict[str, Any]]] = {}
        for fixture in normalized:
            groups.setdefault(fixture["conversation_key"], []).append(fixture)
        started = self._utcnow().astimezone(UTC)
        response_references: list[str] = []
        self._traffic_ledger.mark_started(
            agent_name,
            now=started,
            uncertain_seconds=TRAFFIC_UNCERTAINTY_SECONDS,
        )
        completed_groups: dict[
            str,
            list[
                tuple[
                    int,
                    list[str],
                    bool,
                    int,
                    int,
                    int,
                    int,
                    tuple[SemanticAssertionEvidence, ...],
                    bool,
                ]
            ],
        ] = {}
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=min(5, len(groups))) as pool:
            futures = {
                pool.submit(
                    self._invoke_group,
                    agent_name,
                    agent_type,
                    foundry_version,
                    fixtures,
                    seed,
                ): key
                for key, fixtures in groups.items()
            }
            for future in as_completed(futures):
                try:
                    completed_groups[futures[future]] = future.result()
                except Exception as error:
                    errors.append(error)
        if errors:
            if all(
                re.search(r"\bHTTP [0-9]{3}\b", str(error))
                for error in errors
            ):
                self._traffic_ledger.mark_completed(
                    agent_name,
                    now=self._utcnow().astimezone(UTC),
                )
            primary = next(
                (
                    error
                    for error in errors
                    if re.search(r"\bHTTP [0-9]{3}\b", str(error)) is None
                ),
                errors[0],
            )
            raise primary
        self._traffic_ledger.mark_completed(
            agent_name,
            now=self._utcnow().astimezone(UTC),
        )
        ordered = sorted(
            [
                item
                for values in completed_groups.values()
                for item in values
            ],
            key=lambda item: item[0],
        )
        usable_response_count = 0
        semantic_assertion_count = 0
        semantic_assertions_passed = 0
        request_summaries = []
        for (
            request_index,
            references,
            usable,
            assertion_count,
            assertions_passed,
            direct_terminal_response_count,
            function_call_count,
            assertion_results,
            activation_gate,
        ) in ordered:
            response_references.extend(references)
            usable_response_count += int(usable)
            semantic_assertion_count += assertion_count
            semantic_assertions_passed += assertions_passed
            request_summaries.append(
                RequestCompletionEvidence(
                    request_index=request_index,
                    response_count=len(references),
                    usable_response=usable,
                    semantic_assertion_count=assertion_count,
                    semantic_assertions_passed=assertions_passed,
                    assertion_results=assertion_results,
                    activation_gate=activation_gate,
                    direct_terminal_response_count=direct_terminal_response_count,
                    function_call_count=function_call_count,
                )
            )
        _validate_response_references(tuple(response_references), len(requests))
        completed = self._utcnow().astimezone(UTC)
        return InvocationEvidence(
            operation_ids=(),
            response_references=tuple(response_references),
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            request_count=len(requests),
            allow_window_correlation=False,
            response_count=len(ordered),
            usable_response_count=usable_response_count,
            semantic_assertion_count=semantic_assertion_count,
            semantic_assertions_passed=semantic_assertions_passed,
            request_summaries=tuple(request_summaries),
        )

    def _invoke_group(
        self,
        agent_name: str,
        agent_type: str,
        foundry_version: str,
        fixtures: list[dict[str, Any]],
        seed: int,
    ) -> list[
        tuple[
            int,
            list[str],
            bool,
            int,
            int,
            int,
            int,
            tuple[SemanticAssertionEvidence, ...],
            bool,
        ]
    ]:
        if agent_type == "prompt":
            results: list[
                tuple[
                    int,
                    list[str],
                    bool,
                    int,
                    int,
                    int,
                    int,
                    tuple[SemanticAssertionEvidence, ...],
                    bool,
                ]
            ] = []
            previous_response_id: str | None = None
            for fixture in fixtures:
                (
                    response_ids,
                    usable,
                    assertion_count,
                    assertions_passed,
                    direct_terminal_response_count,
                    function_call_count,
                    assertion_results,
                    activation_gate,
                ) = self._invoke_prompt(
                    agent_name,
                    foundry_version,
                    fixture,
                    seed + int(fixture["_index"]),
                    previous_response_id,
                )
                previous_response_id = response_ids[-1]
                results.append(
                    (
                        int(fixture["_index"]),
                        response_ids,
                        usable,
                        assertion_count,
                        assertions_passed,
                        direct_terminal_response_count,
                        function_call_count,
                        assertion_results,
                        activation_gate,
                    )
                )
            return results
        self._activate_hosted_version(agent_name, foundry_version)
        session_id = self._create_hosted_session(agent_name, foundry_version)
        try:
            results = [
                (
                    int(fixture["_index"]),
                    *self._invoke_hosted(
                        agent_name,
                        session_id,
                        fixture,
                        seed + int(fixture["_index"]),
                    ),
                )
                for fixture in fixtures
            ]
        except Exception:
            try:
                self._delete_hosted_session(agent_name, session_id)
            except Exception:
                self.report_progress(
                    f"{agent_name}/{foundry_version}: session cleanup also failed"
                )
            raise
        try:
            self._delete_hosted_session(agent_name, session_id)
        except Exception:
            self.report_progress(
                f"{agent_name}/{foundry_version}: session cleanup failed after "
                "endpoint completion; preserving completed evidence"
            )
        return results

    def _delete_hosted_session(
        self,
        agent_name: str,
        session_id: str,
    ) -> None:
        self._json_request(
            "DELETE",
            f"{self._profile.project_endpoint}/agents/"
            f"{urllib.parse.quote(agent_name, safe='')}/endpoint/sessions/"
            f"{urllib.parse.quote(session_id, safe='')}",
            hosted=True,
            expected={200, 202, 204, 404},
            retry_statuses=_TRANSIENT_HTTP,
        )

    def _activate_hosted_version(
        self,
        agent_name: str,
        foundry_version: str,
    ) -> None:
        response = self._json_request(
            "PATCH",
            f"{self._profile.project_endpoint}/agents/"
            f"{urllib.parse.quote(agent_name, safe='')}",
            {
                "agent_endpoint": {
                    "version_selector": {
                        "version_selection_rules": [
                            {
                                "agent_version": foundry_version,
                                "traffic_percentage": 100,
                                "type": "FixedRatio",
                            }
                        ]
                    }
                }
            },
            hosted=True,
            expected={200},
            content_type="application/merge-patch+json",
            retry_statuses=_TRANSIENT_HTTP,
            retry_no_response=True,
        )
        rules = (
            response.get("agent_endpoint", {})
            .get("version_selector", {})
            .get("version_selection_rules", [])
        )
        if not any(
            str(rule.get("agent_version") or "") == foundry_version
            and int(rule.get("traffic_percentage") or 0) == 100
            for rule in rules
            if isinstance(rule, dict)
        ):
            raise ContractError("Hosted endpoint did not confirm exact-version routing")

    def _invoke_prompt(
        self,
        agent_name: str,
        foundry_version: str,
        fixture: dict[str, Any],
        seed: int,
        previous_response_id: str | None,
        *,
        include_seed_metadata: bool = True,
        validation_intent_reference: str | None = None,
    ) -> tuple[
        list[str],
        bool,
        int,
        int,
        int,
        int,
        tuple[SemanticAssertionEvidence, ...],
        bool,
    ]:
        if "text" in fixture["body"]:
            raise ContractError(
                "Prompt traffic cannot contain unsupported request-side text formatting"
            )
        reference = {
            "type": "agent_reference",
            "name": agent_name,
            "version": foundry_version,
        }
        body = dict(fixture["body"])
        body.pop("conversation", None)
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        body["store"] = True
        body["agent_reference"] = reference
        if include_seed_metadata:
            body["metadata"] = {
                **body.get("metadata", {}),
                "traffic_seed": str(seed),
            }
        if validation_intent_reference is not None:
            body["metadata"] = {
                **body.get("metadata", {}),
                "validation_intent_reference": validation_intent_reference,
            }
        self._traffic_ledger.mark_started(
            agent_name,
            now=self._utcnow().astimezone(UTC),
            uncertain_seconds=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        propagation_retry = 0
        while True:
            try:
                response = self._json_request(
                    "POST",
                    f"{self._profile.project_endpoint}/openai/v1/responses",
                    body,
                    expected={fixture["expected_status"]},
                )
                break
            except RemoteOperationError as error:
                if (
                    not previous_response_id
                    or not _previous_response_propagation_pending(error)
                    or propagation_retry
                    == len(_PROMPT_RESPONSE_PROPAGATION_RETRY_DELAYS)
                ):
                    raise
                delay = _PROMPT_RESPONSE_PROPAGATION_RETRY_DELAYS[
                    propagation_retry
                ]
                propagation_retry += 1
                self.report_progress(
                    f"{agent_name}/{foundry_version}: prior response is not yet "
                    f"available; retrying chained request in {delay}s"
                )
                self._sleep(delay)
        response_id = str(response.get("id") or "")
        if not response_id:
            raise RemoteOperationError(
                "Prompt response identity is missing",
                code="prompt_response_identity_missing",
                status=int(response.get("_http_status") or fixture["expected_status"]),
                request_accepted=True,
            )
        calls = [
            value
            for value in response.get("output", [])
            if isinstance(value, dict) and value.get("type") == "function_call"
        ]
        function_call_count = len(calls)
        if function_call_count:
            raise RemoteOperationError(
                "Prompt emitted a function call; pure Prompt traffic requires one "
                "direct terminal response",
                code="prompt_function_call",
                status=int(response.get("_http_status") or fixture["expected_status"]),
                request_accepted=True,
            )
        assertion_count, assertions_passed, assertion_results = (
            _semantic_assertion_result(
                response,
                fixture,
            )
        )
        usable = _usable_response(response, fixture["expected_status"])
        return (
            [response_id],
            usable,
            assertion_count,
            assertions_passed,
            int(bool(_response_text(response))),
            function_call_count,
            assertion_results,
            bool(fixture.get("activation_gate", False)),
        )

    def _create_hosted_session(
        self,
        agent_name: str,
        foundry_version: str,
        *,
        validation_intent_reference: str | None = None,
    ) -> str:
        session = self._json_request(
            "POST",
            f"{self._profile.project_endpoint}/agents/"
            f"{urllib.parse.quote(agent_name, safe='')}/endpoint/sessions",
            {
                "version_indicator": {
                    "type": "version_ref",
                    "agent_version": foundry_version,
                },
                **(
                    {
                        "metadata": {
                            "validation_intent_reference": (
                                validation_intent_reference
                            )
                        }
                    }
                    if validation_intent_reference is not None
                    else {}
                ),
            },
            hosted=True,
        )
        session_id = str(
            session.get("agent_session_id")
            or session.get("session_id")
            or session.get("id")
            or ""
        )
        indicator = session.get("version_indicator")
        if (
            not session_id
            or not isinstance(indicator, dict)
            or indicator.get("type") != "version_ref"
            or str(indicator.get("agent_version") or "") != foundry_version
        ):
            raise RemoteOperationError(
                "Hosted session did not bind to the exact version",
                code="hosted_session_version_mismatch",
                status=int(session.get("_http_status") or 200),
                request_accepted=True,
            )
        return session_id

    def _invoke_hosted(
        self,
        agent_name: str,
        session_id: str,
        fixture: dict[str, Any],
        seed: int,
        *,
        validation_intent_reference: str | None = None,
    ) -> tuple[
        list[str],
        bool,
        int,
        int,
        int,
        int,
        tuple[SemanticAssertionEvidence, ...],
        bool,
    ]:
        del seed
        body = {
            "input": fixture["body"]["input"],
            "agent_session_id": session_id,
            "store": False,
            **(
                {
                    "metadata": {
                        "validation_intent_reference": validation_intent_reference
                    }
                }
                if validation_intent_reference is not None
                else {}
            ),
        }
        correlation_id = str(uuid.uuid4())
        self._traffic_ledger.mark_started(
            agent_name,
            now=self._utcnow().astimezone(UTC),
            uncertain_seconds=_HOSTED_RESPONSE_TIMEOUT_SECONDS,
        )
        response = self._json_request(
            "POST",
            f"{self._profile.project_endpoint}/agents/"
            f"{urllib.parse.quote(agent_name, safe='')}"
            "/endpoint/protocols/openai/responses",
            body,
            hosted=True,
            expected={fixture["expected_status"]},
            correlation_id=correlation_id,
            timeout_seconds=_HOSTED_RESPONSE_TIMEOUT_SECONDS,
        )
        response_reference = response.get("id")
        if (
            not isinstance(response_reference, str)
            or _RESPONSE_REFERENCE.fullmatch(response_reference) is None
        ):
            raise RemoteOperationError(
                "Hosted response identity is missing or invalid",
                code="hosted_response_identity_invalid",
                status=int(response.get("_http_status") or fixture["expected_status"]),
                request_accepted=True,
            )
        assertion_count, assertions_passed, assertion_results = (
            _semantic_assertion_result(
                response,
                fixture,
            )
        )
        return (
            [response_reference],
            _usable_response(response, fixture["expected_status"]),
            assertion_count,
            assertions_passed,
            0,
            0,
            assertion_results,
            bool(fixture.get("activation_gate", False)),
        )

    def wait_for_telemetry(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        invocation: InvocationEvidence,
    ) -> tuple[str, ...]:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Live telemetry requires installation with ".[azure]"'
            ) from error
        client = self._logs_client()
        start = datetime.fromisoformat(invocation.started_at)
        traffic_end = datetime.fromisoformat(invocation.completed_at)
        query_end = traffic_end + timedelta(minutes=15)
        _validate_response_references(
            invocation.response_references,
            invocation.request_count,
        )
        escaped = ", ".join(
            f'"{value.replace(chr(34), chr(92) + chr(34))}"'
            for value in invocation.response_references
        )
        query = f"""
union traces, dependencies, requests
| where timestamp >= datetime({start.astimezone(UTC).isoformat()})
| extend response_id = coalesce(
    tostring(customDimensions["gen_ai.response.id"]),
    tostring(customDimensions["azure.ai.agentserver.response_id"]),
    tostring(customDimensions["response_id"]))
| extend request_id = coalesce(
    tostring(customDimensions["x-ms-client-request-id"]),
    tostring(customDimensions["client_request_id"]),
    tostring(customDimensions["request_id"]))
| extend agent_version = tostring(customDimensions["gen_ai.agent.version"])
| extend matched_reference = case(
    response_id in ({escaped}), response_id,
    request_id in ({escaped}), request_id,
    "")
| where matched_reference in ({escaped}) and agent_version == "{foundry_version}"
| summarize matched_references=make_set(matched_reference) by operation_Id
"""
        deadline = self._monotonic() + 15 * 60
        next_progress = self._monotonic() + 60
        while self._monotonic() < deadline:
            result = self._query_resource(
                client,
                query,
                timespan=(start, query_end),
            )
            if result.status == LogsQueryStatus.SUCCESS and result.tables:
                complete = _complete_operation_ids(
                    result.tables,
                    invocation.response_references,
                )
                if complete is not None:
                    return complete
                if _operation_correlation_impossible(
                    result.tables,
                    invocation.response_references,
                ):
                    raise ContractError(
                        "Natural telemetry response correlation is ambiguous"
                    )
            if self._monotonic() >= next_progress:
                elapsed = int(15 * 60 - max(deadline - self._monotonic(), 0))
                self.report_progress(
                    f"{agent_name}/{foundry_version}: waiting for telemetry "
                    f"({elapsed}s)"
                )
                next_progress = self._monotonic() + 60
            self._sleep(15)
        raise ContractError("Natural telemetry did not arrive before the bounded deadline")

    def telemetry_identity_passes(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        invocation: InvocationEvidence,
    ) -> tuple[bool, ...]:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Live telemetry requires installation with ".[azure]"'
            ) from error
        _validate_operation_references(operation_ids, invocation.request_count)
        start = datetime.fromisoformat(invocation.started_at).astimezone(UTC)
        traffic_end = datetime.fromisoformat(invocation.completed_at).astimezone(UTC)
        query_end = traffic_end + timedelta(minutes=15)
        values = ", ".join(f'"{value}"' for value in operation_ids)
        query = f"""
union traces, dependencies, requests
| where timestamp >= datetime({start.isoformat()})
  and timestamp < datetime({query_end.isoformat()})
  and operation_Id in ({values})
| extend operation_name=tostring(customDimensions["gen_ai.operation.name"])
| extend observed_agent=tostring(customDimensions["gen_ai.agent.name"])
| extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
| where operation_name == "invoke_agent"
| summarize
    agent_names=make_set(observed_agent),
    agent_versions=make_set(agent_version)
  by operation_Id
"""
        result = self._query_resource(
            self._logs_client(),
            query,
            timespan=(start, query_end),
        )
        if result.status != LogsQueryStatus.SUCCESS:
            raise ContractError("Exact validation telemetry identity query failed")
        observed: dict[str, tuple[set[str], set[str]]] = {}
        for table in result.tables:
            for row in table.rows:
                operation_id = str(row[0]).lower()
                if operation_id not in operation_ids:
                    continue
                observed[operation_id] = (
                    _telemetry_string_set(row[1] if len(row) > 1 else []),
                    _telemetry_string_set(row[2] if len(row) > 2 else []),
                )
        expected = ({agent_name}, {foundry_version})
        return tuple(observed.get(operation_id) == expected for operation_id in operation_ids)

    def start_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: float,
        start_margin_seconds: int,
        persist: Callable[[InsightRunCheckpoint], None],
    ) -> InsightRunCheckpoint:
        earliest, _ = self._operation_time_bounds(
            agent_name=agent_name,
            foundry_version=foundry_version,
            operation_ids=operation_ids,
        )
        start_deadline = (
            earliest
            + timedelta(hours=lookback_hours)
            - timedelta(seconds=start_margin_seconds)
        )
        self._remaining_insight_start_seconds(start_deadline)
        checkpoint = self._start_insights_once(
            monitor_id=monitor_id,
            lookback_hours=lookback_hours,
            start_deadline=start_deadline,
        )
        persist(checkpoint)
        return checkpoint

    def finish_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        checkpoint: InsightRunCheckpoint,
        validate_window: bool = True,
    ) -> InsightRunEvidence:
        run = self._wait_insights_run(
            agent_name,
            foundry_version,
            monitor_id,
            checkpoint.run_id,
        )
        after = self._list_insights(monitor_id)
        changed = [
            item
            for item in after
            if checkpoint.before_revisions.get(str(item.get("id") or ""))
            != (
                str(item.get("updated_at") or item.get("updatedAt") or ""),
                len(self._linked_ids(item)),
            )
        ]
        evidence = tuple(
            self._to_insight(value)
            for value in changed
            if str(value.get("agent_version") or value.get("agentVersion") or "")
            == foundry_version
            and linked_operations_match_scope(
                self._linked_ids(value),
                operation_ids,
            )
        )
        result = InsightRunEvidence(
            run_reference=_opaque(checkpoint.run_id),
            window_start=str(run.get("window_start") or run.get("windowStart") or ""),
            window_end=str(run.get("window_end") or run.get("windowEnd") or ""),
            status=str(run.get("status") or ""),
            insights=evidence,
        )
        if validate_window and result.status.lower() == "succeeded":
            earliest, latest = self._operation_time_bounds(
                agent_name=agent_name,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
            )
            self._assert_run_contains_operations(result, earliest, latest)
        return result

    def _start_insights_once(
        self,
        *,
        monitor_id: str,
        lookback_hours: float,
        start_deadline: datetime | None = None,
    ) -> InsightRunCheckpoint:
        before = self._insight_revisions(monitor_id)
        if start_deadline is not None:
            self._remaining_insight_start_seconds(start_deadline)
        service_lookback: int | float = (
            int(lookback_hours)
            if float(lookback_hours).is_integer()
            else lookback_hours
        )
        timeout_seconds: int | float = _DEFAULT_REQUEST_TIMEOUT_SECONDS
        if start_deadline is not None:
            timeout_seconds = min(
                timeout_seconds,
                self._remaining_insight_start_seconds(start_deadline),
            )
        run = self._json_request(
            "POST",
            self._insights_url(
                f"/agent_insight_monitors/{urllib.parse.quote(monitor_id, safe='')}/runs"
            ),
            {"lookback_hours": service_lookback},
            expected={200, 201, 202},
            timeout_seconds=timeout_seconds,
            request_deadline=start_deadline,
        )
        run_id = str(run.get("id") or "")
        if not run_id:
            raise ContractError("Agent Insights run omitted its identity")
        return InsightRunCheckpoint(
            run_id=run_id,
            before_revisions=before,
        )

    def _remaining_insight_start_seconds(self, deadline: datetime) -> float:
        remaining = (
            deadline.astimezone(UTC) - self._utcnow().astimezone(UTC)
        ).total_seconds()
        if remaining <= 0:
            raise InsightWindowExpiredError(
                "Correlated operations expired into the guarded Agent Insights start margin"
            )
        return remaining

    def _wait_insights_run(
        self,
        agent_name: str,
        foundry_version: str,
        monitor_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 45 * 60
        next_progress = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = self._json_request(
                "GET",
                self._insights_url(
                    f"/agent_insight_monitors/{urllib.parse.quote(monitor_id, safe='')}"
                    f"/runs/{urllib.parse.quote(run_id, safe='')}"
                ),
            )
            status = str(run.get("status") or "").lower()
            if status in {"succeeded", "failed", "canceled"}:
                break
            if time.monotonic() >= next_progress:
                self.report_progress(
                    f"{agent_name}/{foundry_version}: Agent Insights run status "
                    f"{status or 'pending'}"
                )
                next_progress = time.monotonic() + 60
            self._sleep(10)
        else:
            raise ContractError("Agent Insights run exceeded its bounded deadline")
        return run

    def _operation_time_bounds(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
    ) -> tuple[datetime, datetime]:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Operation window verification requires installation with ".[azure]"'
            ) from error
        values = ", ".join(f'"{value}"' for value in operation_ids)
        query = f"""
union traces, dependencies, requests
| where operation_Id in ({values})
| extend operation_name=tostring(customDimensions["gen_ai.operation.name"])
| extend observed_agent=tostring(customDimensions["gen_ai.agent.name"])
| extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
| where operation_name == "invoke_agent"
  and observed_agent == "{agent_name}"
  and agent_version == "{foundry_version}"
| summarize earliest=min(timestamp), latest=max(timestamp), roots=dcount(operation_Id)
"""
        result = self._query_resource(
            self._logs_client(),
            query,
            timespan=timedelta(days=90),
        )
        if (
            result.status != LogsQueryStatus.SUCCESS
            or not result.tables
            or not result.tables[0].rows
        ):
            raise ContractError("Operation window verification query failed")
        row = result.tables[0].rows[0]
        if (
            not isinstance(row[0], datetime)
            or not isinstance(row[1], datetime)
            or int(row[2] or 0) != len(operation_ids)
        ):
            raise ContractError("Operation window verification evidence is incomplete")
        return row[0].astimezone(UTC), row[1].astimezone(UTC)

    @staticmethod
    def _assert_run_contains_operations(
        result: InsightRunEvidence,
        earliest: datetime,
        latest: datetime,
    ) -> None:
        try:
            start = datetime.fromisoformat(result.window_start).astimezone(UTC)
            end = datetime.fromisoformat(result.window_end).astimezone(UTC)
        except (TypeError, ValueError) as error:
            raise ContractError("Agent Insights run returned an invalid window") from error
        if start > earliest or end <= latest:
            raise InsightWindowExpiredError(
                "Agent Insights run window excluded correlated operations"
            )

    def verify_trace_contract(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        required_operations_by_request: tuple[tuple[str, ...], ...],
        window_start: str,
        window_end: str,
    ) -> None:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Trace verification requires installation with ".[azure]"'
            ) from error
        values = ", ".join(f'"{value}"' for value in operation_ids)
        query = f"""
union traces, dependencies, requests
| where operation_Id in ({values})
| extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
{f'| where agent_version == "{foundry_version}"' if foundry_version else ''}
| extend operation_name=tostring(customDimensions["gen_ai.operation.name"])
| extend observed_agent=tostring(customDimensions["gen_ai.agent.name"])
| extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
| summarize
    operations=make_set(operation_name),
    root_count=countif(
      operation_name == "invoke_agent"
      and observed_agent == "{agent_name}"
      and agent_version == "{foundry_version}"),
    span_count=count()
  by operation_Id
"""
        start = datetime.fromisoformat(window_start)
        end = datetime.fromisoformat(window_end) + timedelta(minutes=15)
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            result = self._query_resource(
                self._logs_client(),
                query,
                timespan=(start, end),
            )
            if (
                result.status == LogsQueryStatus.SUCCESS
                and result.tables
                and _trace_contract_ready(
                    result.tables,
                    operation_ids,
                    required_operations_by_request,
                )
            ):
                return
            self._sleep(15)
        raise ContractError("Trace contract did not stabilize before the bounded deadline")

    def trace_behavior_evidence(
        self,
        operation_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        return _trace_behavior_summary(self._trace_rows(operation_ids))

    def trace_assertion_evidence(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        response_references: tuple[str, ...],
        window_start: str,
        window_end: str,
        traffic_path: Path,
        stabilization_seconds: int,
        on_first_pass: Callable[[], None],
        on_stable: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[tuple[TraceAssertionEvidence, ...], ...]:
        payload = json.loads(traffic_path.read_text(encoding="utf-8"))
        requests = payload if isinstance(payload, list) else payload.get("requests")
        if not isinstance(requests, list):
            raise ContractError("Hosted evidence traffic coverage is inconsistent")
        return LiveRuntime.trace_assertion_evidence_for_requests(
            self,
            agent_name=agent_name,
            foundry_version=foundry_version,
            operation_ids=operation_ids,
            response_references=response_references,
            window_start=window_start,
            window_end=window_end,
            requests=requests,
            stabilization_seconds=stabilization_seconds,
            on_first_pass=on_first_pass,
            on_stable=on_stable,
        )

    def trace_assertion_evidence_for_requests(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        response_references: tuple[str, ...],
        window_start: str,
        window_end: str,
        requests: list[dict[str, Any]],
        stabilization_seconds: int,
        on_first_pass: Callable[[], None],
        on_stable: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[tuple[TraceAssertionEvidence, ...], ...]:
        if stabilization_seconds <= 0:
            raise ContractError("Hosted evidence stabilization interval must be positive")
        if len(requests) != len(response_references):
            raise ContractError("Hosted evidence traffic coverage is inconsistent")
        _validate_response_references(response_references, len(requests))
        _validate_operation_references(operation_ids, len(requests))
        fixtures = tuple(_normalize_fixture(item) for item in requests)
        deadline = self._monotonic() + TRACE_ASSERTION_DEADLINE_SECONDS
        next_progress = self._monotonic() + _TRACE_ASSERTION_PROGRESS_SECONDS
        last_results: tuple[tuple[TraceAssertionEvidence, ...], ...] | None = None
        stable_signature: tuple[
            tuple[tuple[str, str], ...],
            tuple[tuple[tuple[str, bool], ...], ...],
            tuple[str, ...],
        ] | None = None
        stable_since: float | None = None
        first_mapping_observed = False
        passing = False
        correlated: tuple[list[dict[str, Any]], ...] | None = None
        while True:
            rows = self._trace_rows(
                operation_ids,
                response_references,
                foundry_version,
                agent_name,
                window_start,
                window_end,
            )
            correlated = _correlated_request_rows(
                rows,
                response_references,
                operation_ids,
            )
            if _request_correlation_impossible(
                rows,
                response_references,
            ):
                raise TraceAssertionActivationError(
                    "Hosted evidence found ambiguous response-to-operation correlation"
                )
            if correlated is not None:
                if not first_mapping_observed:
                    first_mapping_observed = True
                    on_first_pass()
                last_results = tuple(
                    _trace_assertion_result(request_rows, fixture)
                    for request_rows, fixture in zip(
                        correlated,
                        fixtures,
                        strict=True,
                    )
                )
                passing = all(
                    assertion.passed
                    for request_results in last_results
                    for assertion in request_results
                )
                signature = _trace_assertion_stability_signature(
                    rows,
                    correlated,
                    response_references,
                    last_results,
                )
                now = self._monotonic()
                if signature != stable_signature:
                    stable_signature = signature
                    stable_since = now
                if (
                    passing
                    and stable_since is not None
                    and now - stable_since >= stabilization_seconds
                ):
                    if on_stable is not None:
                        on_stable(_trace_behavior_summary(rows))
                    return last_results
            else:
                passing = False
                stable_signature = None
                stable_since = None
            now = self._monotonic()
            if now >= deadline:
                break
            if now >= next_progress:
                elapsed = int(
                    TRACE_ASSERTION_DEADLINE_SECONDS - max(deadline - now, 0)
                )
                state = (
                    "passing"
                    if passing
                    else "failing"
                    if correlated is not None
                    else "correlation"
                )
                self.report_progress(
                    f"Hosted {state} evidence is stabilizing ({elapsed}s)"
                )
                next_progress = now + _TRACE_ASSERTION_PROGRESS_SECONDS
            self._sleep(min(TRACE_ASSERTION_POLL_SECONDS, deadline - now))
        if (
            correlated is not None
            and last_results is not None
            and not passing
            and stable_since is not None
            and self._monotonic() - stable_since >= stabilization_seconds
        ):
            if on_stable is not None:
                on_stable(_trace_behavior_summary(rows))
            return last_results
        if correlated is not None:
            raise TraceAssertionActivationError(
                "Hosted evidence did not stabilize before the bounded deadline"
            )
        raise TraceAssertionActivationError(
            "Hosted evidence requires exact response-to-operation correlation"
        )

    def _trace_rows(
        self,
        operation_ids: tuple[str, ...],
        response_references: tuple[str, ...] = (),
        foundry_version: str | None = None,
        agent_name: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Trace behavior evidence requires installation with ".[azure]"'
            ) from error
        if not operation_ids or any(
            _TRACE_ID.fullmatch(value) is None for value in operation_ids
        ):
            raise ContractError("Trace behavior evidence has invalid operation identities")
        values = ", ".join(f'"{value}"' for value in operation_ids)
        references = ", ".join(
            f'"{value.replace(chr(34), chr(92) + chr(34))}"'
            for value in response_references
        ) or '""'
        scoped_operations = ""
        operation_filter = f"| where operation_Id in ({values})"
        timespan: timedelta | tuple[datetime, datetime] = timedelta(days=90)
        if response_references:
            if not agent_name or not foundry_version or not window_start or not window_end:
                raise ContractError(
                    "Trace assertion correlation requires exact Agent and invocation scope"
                )
            try:
                start = datetime.fromisoformat(window_start).astimezone(UTC)
                traffic_end = datetime.fromisoformat(window_end).astimezone(UTC)
            except (TypeError, ValueError) as error:
                raise ContractError(
                    "Trace assertion correlation has an invalid invocation window"
                ) from error
            if traffic_end < start:
                raise ContractError(
                    "Trace assertion correlation has an invalid invocation window"
                )
            query_end = traffic_end + timedelta(minutes=15)
            scoped_operations = f"""
let scoped_reference_operations =
    union traces, dependencies, requests
    | where timestamp >= datetime({start.isoformat()})
      and timestamp < datetime({query_end.isoformat()})
    | extend response_id=coalesce(
        tostring(customDimensions["gen_ai.response.id"]),
        tostring(customDimensions["azure.ai.agentserver.response_id"]),
        tostring(customDimensions["response_id"]))
    | extend request_id=coalesce(
        tostring(customDimensions["x-ms-client-request-id"]),
        tostring(customDimensions["client_request_id"]),
        tostring(customDimensions["request_id"]))
    | extend observed_agent=tostring(customDimensions["gen_ai.agent.name"])
    | extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
    | extend matched_reference=case(
        response_id in ({references}), response_id,
        request_id in ({references}), request_id,
        "")
    | where matched_reference in ({references})
      and observed_agent == "{agent_name}"
      and agent_version == "{foundry_version}"
    | distinct operation_Id;
"""
            operation_filter = (
                f"| where operation_Id in ({values}) "
                "or operation_Id in (scoped_reference_operations)"
            )
            timespan = (start, query_end)
        query = f"""
{scoped_operations}
union traces, dependencies, requests
{operation_filter}
| extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
{f'| where agent_version == "{foundry_version}"' if foundry_version else ''}
| extend operation_name=tostring(customDimensions["gen_ai.operation.name"])
| extend tool_name=coalesce(
    tostring(customDimensions["gen_ai.tool.name"]),
    tostring(customDimensions["tool.name"]))
| extend tool_call_id=coalesce(
    tostring(customDimensions["gen_ai.tool.call.id"]),
    tostring(customDimensions["tool.call.id"]))
| extend tool_arguments=coalesce(
    tostring(customDimensions["aiq.tool.call.arguments"]),
    tostring(customDimensions["gen_ai.tool.call.arguments"]))
| extend error_type=tostring(customDimensions["error.type"])
| extend tool_ok=tostring(customDimensions["tool.ok"])
| extend tool_result=coalesce(
    tostring(customDimensions["aiq.tool.call.result"]),
    tostring(customDimensions["gen_ai.tool.call.result"]))
| extend structural_tool=tostring(customDimensions["aiq.tool.call.result"])
| extend input_messages=tostring(customDimensions["gen_ai.input.messages"])
| extend output_messages=tostring(customDimensions["gen_ai.output.messages"])
| extend terminal_success=tostring(customDimensions["aiq.terminal_response.success"])
| extend terminal_output=tostring(customDimensions["aiq.terminal_response.output_present"])
| extend handled_error=tostring(customDimensions["aiq.tool.error.handled"])
| extend response_id=coalesce(
    tostring(customDimensions["gen_ai.response.id"]),
    tostring(customDimensions["azure.ai.agentserver.response_id"]),
    tostring(customDimensions["response_id"]))
| extend request_id=coalesce(
    tostring(customDimensions["x-ms-client-request-id"]),
    tostring(customDimensions["client_request_id"]),
    tostring(customDimensions["request_id"]))
| extend matched_reference=case(
    response_id in ({references}), response_id,
    request_id in ({references}), request_id,
    "")
| project operation_Id, operation_name, tool_name, tool_call_id, error_type, tool_ok, tool_result,
    tool_arguments, structural_tool, input_messages, output_messages, timestamp, duration, name,
    terminal_success, terminal_output, handled_error, matched_reference
"""
        result = self._query_resource(
            self._logs_client(),
            query,
            timespan=timespan,
        )
        if result.status != LogsQueryStatus.SUCCESS:
            raise ContractError("Trace behavior evidence query failed")
        rows = [
            {
                "operation_id": str(row[0]),
                "operation_name": str(row[1] or ""),
                "tool_name": str(row[2] or ""),
                "tool_call_id": str(row[3] or ""),
                "error_type": str(row[4] or ""),
                "tool_ok": str(row[5] or ""),
                "tool_result": str(row[6] or ""),
                "tool_arguments": str(row[7] or ""),
                "structural_tool": str(row[8] or ""),
                "messages": [str(row[9] or ""), str(row[10] or "")],
                "timestamp": str(row[11] or ""),
                "duration": row[12],
                "span_name": str(row[13] or ""),
                "terminal_success": str(row[14] or ""),
                "terminal_output": str(row[15] or ""),
                "handled_error": str(row[16] or ""),
                "matched_reference": str(row[17] or ""),
            }
            for table in result.tables
            for row in table.rows
        ]
        return rows

    def replay_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        evidence = []
        for agent in manifest["agents"]:
            for value in [agent["baseline"], *agent["issues"]]:
                operation_ids = value.get("operation_ids") or []
                if operation_ids:
                    found = self._query_retained_operations(
                        tuple(operation_ids),
                        str(value.get("window_start") or ""),
                        str(value.get("window_end") or ""),
                    )
                    evidence.append(
                        {
                            "agent": agent["name"],
                            "logical_version": value["logical_version"],
                            "operation_count": len(operation_ids),
                            "retained_operation_count": len(found),
                            "complete": set(found) == set(operation_ids),
                            "window_start": value.get("window_start"),
                            "window_end": value.get("window_end"),
                        }
                    )
        return {"run_id": manifest["run_id"], "evidence": evidence}

    def _query_retained_operations(
        self,
        operation_ids: tuple[str, ...],
        window_start: str,
        window_end: str,
    ) -> tuple[str, ...]:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Read-only replay requires installation with ".[azure]"'
            ) from error
        if not window_start or not window_end:
            raise ContractError("Replay evidence is missing its exact window")
        start = datetime.fromisoformat(window_start)
        end = datetime.fromisoformat(window_end)
        values = ", ".join(f'"{value}"' for value in operation_ids)
        query = f"""
union traces, dependencies, requests
| where timestamp >= datetime({start.astimezone(UTC).isoformat()})
  and timestamp < datetime({end.astimezone(UTC).isoformat()})
| where operation_Id in ({values})
| summarize by operation_Id
"""
        result = self._query_resource(
            self._logs_client(),
            query,
            timespan=(start, end),
        )
        if result.status != LogsQueryStatus.SUCCESS:
            raise ContractError("Read-only replay telemetry query failed")
        return tuple(
            sorted(
                {
                    str(row[0]).lower()
                    for table in result.tables
                    for row in table.rows
                    if _TRACE_ID.fullmatch(str(row[0]).lower())
                }
            )
        )

    def _insight_revisions(self, monitor_id: str) -> dict[str, tuple[str, int]]:
        return {
            str(item.get("id") or ""): (
                str(item.get("updated_at") or item.get("updatedAt") or ""),
                len(self._linked_ids(item)),
            )
            for item in self._list_insights(monitor_id)
        }

    def _list_insights(self, monitor_id: str) -> list[dict[str, Any]]:
        url = self._insights_url(
            f"/agent_insight_monitors/{urllib.parse.quote(monitor_id, safe='')}"
            "/insights?include_details=true&limit=100"
        )
        values: list[dict[str, Any]] = []
        for _ in range(5):
            payload = self._json_request("GET", url)
            page = payload.get("data") or payload.get("value") or payload.get("items") or []
            values.extend(item for item in page if isinstance(item, dict))
            next_link = payload.get("next_link") or payload.get("nextLink")
            if not next_link:
                return values
            url = urllib.parse.urljoin(
                self._profile.insights_endpoint + "/",
                str(next_link),
            )
        raise ContractError("Agent Insights pagination exceeded the bounded page limit")

    @staticmethod
    def _linked_ids(value: dict[str, Any]) -> tuple[str, ...]:
        details = value.get("details")
        if not isinstance(details, dict):
            return ()
        linked = [
            *(
                details.get("linked_traces")
                or details.get("linkedTraces")
                or []
            ),
            *(
                details.get("highlighted_traces")
                or details.get("highlightedTraces")
                or []
            ),
        ]
        return tuple(
            sorted(
                {
                    str(item.get("trace_id") or item.get("traceId") or "").lower()
                    for item in linked
                    if isinstance(item, dict)
                    and _TRACE_ID.fullmatch(
                        str(item.get("trace_id") or item.get("traceId") or "").lower()
                    )
                }
            )
        )

    def _to_insight(self, value: dict[str, Any]) -> InsightEvidence:
        details = value.get("details")
        actions = (
            details.get("recommended_actions") or details.get("recommendedActions")
            if isinstance(details, dict)
            else None
        )
        fix = (
            actions.get("proposed_fix") or actions.get("proposedFix")
            if isinstance(actions, dict)
            else None
        )
        linked_ids = self._linked_ids(value)
        return InsightEvidence(
            reference=_opaque(str(value.get("id") or "")),
            agent_version=str(value.get("agent_version") or value.get("agentVersion") or ""),
            title=str(value.get("title") or ""),
            description=str(value.get("description") or ""),
            category=str(value.get("category") or ""),
            severity=str(value.get("severity") or ""),
            proposed_fix=str(fix.get("text") or "") if isinstance(fix, dict) else "",
            linked_operation_ids=linked_ids,
            trace_count=len(linked_ids),
            updated_at=str(value.get("updated_at") or value.get("updatedAt") or ""),
        )

    def _insights_url(self, path: str) -> str:
        separator = "&" if "?" in path else "?"
        return f"{self._profile.insights_endpoint}{path}{separator}api-version=v1"

    def _json_request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        *,
        hosted: bool = False,
        expected: set[int] | None = None,
        correlation_id: str | None = None,
        content_type: str = "application/json",
        retry_statuses: set[int] | None = None,
        retry_no_response: bool = False,
        timeout_seconds: int | float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        request_deadline: datetime | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer "
            + self._token_provider(_FOUNDRY_SCOPE),
        }
        if data is not None:
            headers["Content-Type"] = content_type
        request_reference = correlation_id or str(uuid.uuid4())
        headers["x-ms-client-request-id"] = request_reference
        if hosted:
            headers["Foundry-Features"] = "HostedAgents=V1Preview"
        if hosted:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}api-version=v1"
        retries = (
            retry_statuses
            if retry_statuses is not None
            else _TRANSIENT_HTTP
            if method == "GET"
            else set()
        )
        max_attempts = 20 if method == "GET" else 10 if retries else 1
        attempt = 0
        no_response_failures = 0
        credential_refreshed = False
        status = 0
        payload = b""
        response_headers: Mapping[str, str] = {}
        while attempt < max_attempts:
            request = urllib.request.Request(
                url,
                data=data,
                headers=headers,
                method=method,
            )
            attempt_timeout = timeout_seconds
            if request_deadline is not None:
                attempt_timeout = min(
                    attempt_timeout,
                    self._remaining_insight_start_seconds(request_deadline),
                )
            try:
                with self._progress.heartbeat(f"remote {method} request"):
                    with urllib.request.urlopen(
                        request,
                        timeout=attempt_timeout,
                    ) as response:
                        status = response.status
                        payload = response.read()
                        response_headers = getattr(response, "headers", {})
            except urllib.error.HTTPError as error:
                status = error.code
                payload = error.read()
                response_headers = getattr(error, "headers", {}) or {}
            except (TimeoutError, urllib.error.URLError) as error:
                attempt += 1
                no_response_failures += 1
                can_retry = attempt < max_attempts and (
                    method == "GET"
                    or (retry_no_response and no_response_failures < 3)
                )
                if can_retry:
                    request_reference = str(uuid.uuid4())
                    headers["x-ms-client-request-id"] = request_reference
                    delay = min(2 ** (attempt - 1), 30)
                    self.report_progress(
                        f"remote {method} had no response; retrying in {delay}s "
                        f"({attempt + 1}/{max_attempts})"
                    )
                    self._sleep(delay)
                    continue
                raise RemoteOperationError(
                    "Remote operation failed before a response was received",
                    code="remote_no_response",
                    status=None,
                    request_accepted=None,
                ) from error
            if status == 401 and not credential_refreshed:
                self._invalidate_token(_FOUNDRY_SCOPE)
                headers["Authorization"] = (
                    "Bearer " + self._token_provider(_FOUNDRY_SCOPE)
                )
                request_reference = str(uuid.uuid4())
                headers["x-ms-client-request-id"] = request_reference
                credential_refreshed = True
                self.report_progress("remote credential expired; refreshed once")
                continue
            attempt += 1
            if status not in retries or attempt == max_attempts:
                break
            request_reference = str(uuid.uuid4())
            headers["x-ms-client-request-id"] = request_reference
            delay = min(2 ** (attempt - 1), 30)
            self.report_progress(
                f"remote {method} returned HTTP {status}; retrying in {delay}s "
                f"({attempt + 1}/{max_attempts})"
            )
            self._sleep(delay)
        feedback = _rate_limit_values(response_headers)
        self._rate_limit_feedback.value = feedback
        allowed = expected or {200, 201, 202}
        if status not in allowed:
            code, _ = _remote_error(payload)
            safe_code = code or f"http_{status}"
            self.report_progress(
                f"remote {method} rejected: status={status}; code={safe_code}"
            )
            raise RemoteOperationError(
                f"Remote operation failed with HTTP {status}"
                + (f" ({safe_code})" if safe_code else ""),
                code=safe_code,
                status=status,
                request_accepted=_http_request_accepted(status),
            )
        if not payload:
            value: Any = {}
        else:
            try:
                value = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise RemoteOperationError(
                    "Remote operation returned invalid JSON",
                    code="invalid_json",
                    status=status,
                    request_accepted=True,
                ) from error
        if not isinstance(value, dict):
            raise RemoteOperationError(
                "Remote operation returned an invalid JSON shape",
                code="invalid_json_shape",
                status=status,
                request_accepted=True,
            )
        value["_http_status"] = status
        value["_request_reference"] = request_reference
        value["_rate_limit"] = feedback
        return value


def _rate_limit_values(
    headers: Mapping[str, str],
) -> dict[str, int | float | None]:
    def integer(name: str) -> int | None:
        raw = headers.get(name)
        try:
            value = int(raw) if raw is not None else None
        except ValueError:
            return None
        return max(0, value) if value is not None else None

    raw_retry = headers.get("Retry-After")
    try:
        retry_after = float(raw_retry) if raw_retry is not None else None
    except ValueError:
        retry_after = None
    if retry_after is not None and retry_after < 0:
        retry_after = None
    return {
        "remaining_requests": integer("x-ratelimit-remaining-requests"),
        "remaining_tokens": integer("x-ratelimit-remaining-tokens"),
        "retry_after_seconds": retry_after,
    }


def _azure_cli_token(scope: str) -> str:
    for attempt in range(5):
        try:
            with _AUTH_PROGRESS.heartbeat(
                f"Azure token request attempt {attempt + 1}/5"
            ) as outcome:
                process = subprocess.run(
                    [
                        azure_cli(),
                        "account",
                        "get-access-token",
                        "--scope",
                        scope,
                        "--query",
                        "accessToken",
                        "--output",
                        "tsv",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if process.returncode != 0:
                    outcome.fail()
        except subprocess.TimeoutExpired:
            if attempt < 4:
                time.sleep(2**attempt)
                continue
            break
        token = process.stdout.strip()
        if process.returncode == 0 and token:
            return token
        if attempt < 4:
            time.sleep(2**attempt)
    raise ContractError("Azure CLI could not provide a short-lived access token")


def _opaque(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _remote_error(payload: bytes) -> tuple[str, str]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "", ""
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return "", ""
    code = re.sub(r"[^A-Za-z0-9_.-]", "", str(error.get("code") or ""))[:80]
    message = re.sub(r"\s+", " ", str(error.get("message") or "")).strip()[:300]
    return code, message


def _http_request_accepted(status: int) -> bool | None:
    if 400 <= status < 500 and status != 408:
        return False
    return None


def _previous_response_propagation_pending(
    error: RemoteOperationError,
) -> bool:
    code = re.sub(r"[^a-z0-9]+", "_", error.code.casefold()).strip("_")
    return (
        error.request_accepted is False
        and error.status in {400, 404}
        and code == "previous_response_not_found"
    )


def _trace_contract_ready(
    tables: list[Any],
    operation_ids: tuple[str, ...],
    required_operations_by_request: tuple[tuple[str, ...], ...],
) -> bool:
    if len(operation_ids) != len(required_operations_by_request):
        return False
    required_by_operation = dict(
        zip(operation_ids, required_operations_by_request, strict=True)
    )
    seen_ids: set[str] = set()
    observed_by_operation: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        for row in table.rows:
            operation_id = str(row[0]).lower()
            if (
                not _TRACE_ID.fullmatch(operation_id)
                or operation_id not in required_by_operation
            ):
                continue
            seen_ids.add(operation_id)
            operations = row[1]
            if isinstance(operations, str):
                operations = json.loads(operations)
            if isinstance(operations, list):
                observed_by_operation[operation_id].update(
                    str(value) for value in operations if value
                )
            if int(row[2]) < 1 or int(row[3]) < 1:
                return False
    return seen_ids == set(operation_ids) and all(
        set(required_by_operation[operation_id])
        <= observed_by_operation[operation_id]
        for operation_id in operation_ids
    )


def _trace_behavior_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    call_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    anonymous_calls: Counter[str] = Counter()
    response_ids: set[str] = set()
    successful_responses: set[str] = set()
    error_codes: Counter[str] = Counter()
    recorded_errors: set[tuple[str, str]] = set()
    assistant_response_operations: set[str] = set()
    explicit_terminal_success_operations: set[str] = set()
    explicit_terminal_output_operations: set[str] = set()
    handled_error_rows = 0
    unhandled_error_rows = 0
    seen_messages: set[tuple[str, str]] = set()
    terminal_snapshots: dict[str, tuple[tuple[str, str], list[Any]]] = {}

    def record_response(value: Any, identity: str, operation_id: str) -> None:
        if isinstance(value, str) and value.strip().startswith(("{", "[")):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=True)
        response_id = f"{operation_id}:{identity or serialized}"
        response_ids.add(response_id)
        codes: list[str] = []

        def find_codes(item: Any) -> None:
            if isinstance(item, dict):
                error = item.get("error")
                if isinstance(error, dict) and error.get("code"):
                    codes.append(str(error["code"]))
                for child in item.values():
                    find_codes(child)
            elif isinstance(item, list):
                for child in item:
                    find_codes(child)

        find_codes(value)
        if codes:
            for code in codes:
                key = (response_id, code)
                if key not in recorded_errors:
                    recorded_errors.add(key)
                    error_codes[code] += 1
        else:
            successful_responses.add(response_id)

    def record_terminal_assistant(value: Any, operation_id: str) -> None:
        if not isinstance(value, list) or not value:
            return
        message = value[-1]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        parts = message.get("parts")
        if not isinstance(parts, list):
            return
        if any(
            isinstance(part, dict) and part.get("type") == "tool_call"
            for part in parts
        ):
            return
        visible = any(
            isinstance(part, dict)
            and part.get("type") in {"text", "output_text"}
            and isinstance(part.get("content") or part.get("text"), str)
            and str(part.get("content") or part.get("text")).strip()
            for part in parts
        )
        if visible:
            assistant_response_operations.add(operation_id)

    def walk(value: Any, operation_id: str) -> None:
        if isinstance(value, dict):
            value_type = str(value.get("type") or "")
            if value_type == "tool_call":
                tool_name = str(value.get("name") or "unknown")
                call_id = str(value.get("id") or "")
                if call_id:
                    call_names[(operation_id, call_id)].add(tool_name)
                else:
                    anonymous_calls[tool_name] += 1
            elif value_type == "tool_call_response":
                record_response(
                    value.get("response"),
                    str(value.get("id") or value.get("call_id") or ""),
                    operation_id,
                )
            for child in value.values():
                walk(child, operation_id)
        elif isinstance(value, list):
            for child in value:
                walk(child, operation_id)
        elif isinstance(value, str):
            stripped = value.strip()
            message_key = (operation_id, stripped)
            if stripped.startswith(("{", "[")) and message_key not in seen_messages:
                seen_messages.add(message_key)
                try:
                    parsed = json.loads(stripped)
                    walk(parsed, operation_id)
                except json.JSONDecodeError:
                    pass

    for row_index, row in enumerate(rows):
        operation_id = row["operation_id"]
        handled = row.get("handled_error", "").casefold() == "true"
        if row.get("terminal_success", "").casefold() == "true":
            explicit_terminal_success_operations.add(operation_id)
        if row.get("terminal_output", "").casefold() == "true":
            explicit_terminal_output_operations.add(operation_id)
        if handled and row.get("error_type"):
            handled_error_rows += 1
        elif row.get("error_type"):
            unhandled_error_rows += 1
        if row["operation_name"] == "execute_tool":
            tool_name = row["tool_name"] or "unknown"
            call_id = row.get("tool_call_id", "")
            if call_id:
                call_names[(operation_id, call_id)].add(tool_name)
            else:
                anonymous_calls[tool_name] += 1
            tool_ok = row.get("tool_ok", "").casefold()
            response_id = f"{operation_id}:{call_id or f'span:{row_index}'}"
            if row["error_type"]:
                response_ids.add(response_id)
                key = (response_id, row["error_type"])
                if key not in recorded_errors:
                    recorded_errors.add(key)
                    error_codes[row["error_type"]] += 1
            elif tool_ok == "false":
                response_ids.add(response_id)
                key = (response_id, "tool_error")
                if key not in recorded_errors:
                    recorded_errors.add(key)
                    error_codes["tool_error"] += 1
            elif tool_ok == "true":
                response_ids.add(response_id)
                successful_responses.add(response_id)
            elif row["tool_result"]:
                try:
                    record_response(
                        json.loads(row["tool_result"]),
                        call_id or f"span:{row_index}",
                        operation_id,
                    )
                except json.JSONDecodeError:
                    response_ids.add(response_id)
                    successful_responses.add(response_id)
        for message_index, raw in enumerate(row["messages"]):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                walk(raw, operation_id)
                continue
            walk(parsed, operation_id)
            if message_index == 1 and isinstance(parsed, list):
                candidate_key = (
                    row.get("timestamp", ""),
                    json.dumps(parsed, sort_keys=True, ensure_ascii=True),
                )
                current = terminal_snapshots.get(operation_id)
                if current is None or candidate_key > current[0]:
                    terminal_snapshots[operation_id] = (candidate_key, parsed)

    for operation_id, (_, snapshot) in terminal_snapshots.items():
        record_terminal_assistant(snapshot, operation_id)

    call_counts: Counter[str] = Counter()
    for names in call_names.values():
        canonical = next(
            (name for name in sorted(names) if name != "unknown"),
            "unknown",
        )
        call_counts[canonical] += 1
    call_counts.update(anonymous_calls)
    terminal_success_operations = (
        assistant_response_operations | explicit_terminal_success_operations
    )
    terminal_output_operations = (
        assistant_response_operations | explicit_terminal_output_operations
    )
    return {
        "operation_count": len({row["operation_id"] for row in rows}),
        "tool_call_counts": dict(sorted(call_counts.items())),
        "tool_response_count": len(response_ids),
        "successful_tool_response_count": len(successful_responses),
        "error_codes": dict(sorted(error_codes.items())),
        "assistant_response_count": len(assistant_response_operations),
        "explicit_terminal_success_count": len(
            explicit_terminal_success_operations
        ),
        "explicit_terminal_output_count": len(
            explicit_terminal_output_operations
        ),
        "terminal_success_count": len(terminal_success_operations),
        "terminal_output_count": len(terminal_output_operations),
        "terminal_response_count": len(
            terminal_success_operations & terminal_output_operations
        ),
        "handled_error_count": handled_error_rows,
        "unhandled_error_count": unhandled_error_rows,
    }


def _correlated_request_rows(
    rows: list[dict[str, Any]],
    response_references: tuple[str, ...],
    operation_ids: tuple[str, ...],
) -> tuple[list[dict[str, Any]], ...] | None:
    if (
        len(response_references) != len(operation_ids)
        or len(set(response_references)) != len(response_references)
        or len(set(operation_ids)) != len(operation_ids)
        or any(not value for value in response_references)
    ):
        return None
    allowed_operations = set(operation_ids)
    operations_by_reference: dict[str, set[str]] = defaultdict(set)
    rows_by_operation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        operation_id = str(row.get("operation_id") or "").lower()
        if operation_id not in allowed_operations:
            continue
        rows_by_operation[operation_id].append(row)
        reference = str(row.get("matched_reference") or "")
        if reference in response_references:
            operations_by_reference[reference].add(operation_id)
    ordered_operations: list[str] = []
    for reference in response_references:
        matched = operations_by_reference.get(reference, set())
        if len(matched) != 1:
            return None
        ordered_operations.append(next(iter(matched)))
    if len(set(ordered_operations)) != len(ordered_operations):
        return None
    if set(ordered_operations) != allowed_operations:
        return None
    return tuple(rows_by_operation[operation_id] for operation_id in ordered_operations)


def _request_correlation_impossible(
    rows: list[dict[str, Any]],
    response_references: tuple[str, ...],
) -> bool:
    expected_references = set(response_references)
    operations_by_reference: dict[str, set[str]] = defaultdict(set)
    references_by_operation: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        operation_id = str(row.get("operation_id") or "").lower()
        reference = str(row.get("matched_reference") or "")
        if (
            _TRACE_ID.fullmatch(operation_id) is None
            or reference not in expected_references
        ):
            continue
        operations_by_reference[reference].add(operation_id)
        references_by_operation[operation_id].add(reference)
    return any(len(values) > 1 for values in operations_by_reference.values()) or any(
        len(values) > 1 for values in references_by_operation.values()
    )


def _trace_rows_signature(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            json.dumps(row, sort_keys=True, ensure_ascii=True, default=str)
            for row in rows
        )
    )


def _trace_assertion_stability_signature(
    rows: list[dict[str, Any]],
    correlated: tuple[list[dict[str, Any]], ...],
    response_references: tuple[str, ...],
    results: tuple[tuple[TraceAssertionEvidence, ...], ...],
) -> tuple[
    tuple[tuple[str, str], ...],
    tuple[tuple[tuple[str, bool], ...], ...],
    tuple[str, ...],
]:
    correlation = tuple(
        (
            reference,
            str(request_rows[0].get("operation_id") or "").lower(),
        )
        for reference, request_rows in zip(
            response_references,
            correlated,
            strict=True,
        )
    )
    assertion_results = tuple(
        tuple((assertion.assertion, assertion.passed) for assertion in request_results)
        for request_results in results
    )
    return correlation, assertion_results, _trace_rows_signature(rows)


def _json_trace_value(value: Any) -> Any:
    parsed = value
    for _ in range(2):
        if not isinstance(parsed, str) or not parsed.strip().startswith(("{", "[")):
            break
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            break
    return parsed


def _nested_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _result_class(row: dict[str, Any]) -> str:
    if str(row.get("error_type") or ""):
        return "error"
    tool_ok = str(row.get("tool_ok") or "").casefold()
    result = _json_trace_value(row.get("tool_result"))
    if tool_ok == "false" or (
        isinstance(result, dict) and isinstance(result.get("error"), dict)
    ):
        return "error"
    if tool_ok == "true" or result not in (None, ""):
        return "success"
    return "unknown"


def _tool_rows(
    rows: list[dict[str, Any]],
    tool_name: str,
) -> list[dict[str, Any]]:
    matching = [
            row
            for row in rows
            if row.get("operation_name") == "execute_tool"
            and row.get("tool_name") == tool_name
        ]
    structural = [row for row in matching if row.get("structural_tool")]
    return sorted(
        structural or matching,
        key=lambda row: (str(row.get("timestamp") or ""), str(row.get("span_name") or "")),
    )


def _terminal_text(rows: list[dict[str, Any]]) -> str:
    candidates: list[tuple[str, str]] = []
    for row in rows:
        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            continue
        parsed = _json_trace_value(messages[1])
        if not isinstance(parsed, list) or not parsed:
            continue
        message = parsed[-1]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            parts = message.get("content")
        if not isinstance(parts, list):
            continue
        text = " ".join(
            str(part.get("content") or part.get("text") or "").strip()
            for part in parts
            if isinstance(part, dict)
            and part.get("type") in {"text", "output_text"}
            and str(part.get("content") or part.get("text") or "").strip()
        )
        if text:
            candidates.append((str(row.get("timestamp") or ""), text))
    return max(candidates, default=("", ""))[1]


def _request_text(body: dict[str, Any]) -> str:
    values: list[str] = []
    for message in body.get("input", []):
        if not isinstance(message, dict):
            continue
        for content in message.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                values.append(content["text"])
    return "\n".join(values)


def _scope_values(text: str, scope_kind: str) -> list[str]:
    patterns = {
        "account": r"\bacct-demo-[a-z]+\b",
        "trip": r"\btrip-[a-z]+\b",
    }
    return re.findall(patterns[scope_kind], text.casefold())


def _duration_seconds(value: Any) -> float:
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0
    text = str(value or "")
    match = re.fullmatch(
        r"(?:(\d+)\.)?(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)",
        text,
    )
    if not match:
        return 0.0
    days, hours, minutes, seconds = match.groups()
    return (
        float(days or 0) * 86400
        + float(hours) * 3600
        + float(minutes) * 60
        + float(seconds)
    )


def _span_interval(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.fromisoformat(str(row.get("timestamp") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return start, start + timedelta(seconds=_duration_seconds(row.get("duration")))


_TRACE_ASSERTION_FIELDS = {
    "tool_call_count": {"name", "kind", "tool_name", "count"},
    "tool_argument_presence": {
        "name",
        "kind",
        "tool_name",
        "argument",
        "present",
    },
    "scope_relation": {
        "name",
        "kind",
        "tool_name",
        "scope_kind",
        "request_scope",
        "argument",
        "result_field",
        "request_tool_equal",
        "request_result_equal",
        "tool_result_equal",
    },
    "tool_result_class": {"name", "kind", "tool_name", "result_class"},
    "retry_sequence": {"name", "kind", "tool_name", "result_sequence"},
    "terminal_claim_relation": {
        "name",
        "kind",
        "tool_name",
        "result_class",
        "result_path",
        "relation",
        "required_terms_all",
        "forbidden_terms",
    },
    "payload_multiplicity": {
        "name",
        "kind",
        "source",
        "tool_name",
        "path",
        "minimum",
        "maximum",
    },
    "span_relation": {
        "name",
        "kind",
        "first_tool",
        "second_tool",
        "relation",
    },
    "operation_sequence": {
        "name",
        "kind",
        "operations",
    },
}


def _normalize_trace_assertions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractError("Traffic trace assertions must be an array")
    assertions: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ContractError("Traffic trace assertion must be an object")
        name = raw.get("name")
        kind = raw.get("kind")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None
            or name in names
            or kind not in _TRACE_ASSERTION_FIELDS
            or not set(raw).issubset(_TRACE_ASSERTION_FIELDS[str(kind)])
        ):
            raise ContractError("Traffic trace assertion definition is invalid")
        names.add(name)
        assertion = dict(raw)
        tool_name = assertion.get("tool_name")
        if kind in {
            "tool_call_count",
            "tool_argument_presence",
            "scope_relation",
            "tool_result_class",
            "retry_sequence",
            "terminal_claim_relation",
        } and (not isinstance(tool_name, str) or not tool_name):
            raise ContractError("Traffic trace assertion tool name is invalid")
        if kind == "tool_call_count" and (
            not isinstance(assertion.get("count"), int)
            or isinstance(assertion["count"], bool)
            or assertion["count"] < 0
        ):
            raise ContractError("Traffic trace assertion count is invalid")
        if kind == "tool_argument_presence" and (
            not isinstance(assertion.get("argument"), str)
            or not assertion["argument"]
            or not isinstance(assertion.get("present"), bool)
        ):
            raise ContractError("Traffic trace argument assertion is invalid")
        if kind == "scope_relation":
            if (
                assertion.get("scope_kind") not in {"account", "trip"}
                or assertion.get("request_scope") not in {"first", "last"}
                or not isinstance(assertion.get("argument"), str)
                or not assertion["argument"]
                or not isinstance(assertion.get("request_tool_equal"), bool)
            ):
                raise ContractError("Traffic trace scope assertion is invalid")
            for key in ("request_result_equal", "tool_result_equal"):
                if key in assertion and not isinstance(assertion[key], bool):
                    raise ContractError("Traffic trace scope assertion is invalid")
            if any(
                key in assertion
                for key in ("request_result_equal", "tool_result_equal")
            ) and (
                not isinstance(assertion.get("result_field"), str)
                or not assertion["result_field"]
            ):
                raise ContractError("Traffic trace result scope is invalid")
        if kind == "tool_result_class" and assertion.get("result_class") not in {
            "success",
            "error",
        }:
            raise ContractError("Traffic trace result class is invalid")
        if kind == "retry_sequence":
            sequence = assertion.get("result_sequence")
            if (
                not isinstance(sequence, list)
                or not sequence
                or any(item not in {"success", "error"} for item in sequence)
            ):
                raise ContractError("Traffic trace retry sequence is invalid")
        if kind == "operation_sequence":
            operations = assertion.get("operations")
            if (
                not isinstance(operations, list)
                or not operations
                or any(
                    item not in {"invoke_agent", "execute_tool", "chat"}
                    for item in operations
                )
            ):
                raise ContractError("Traffic operation sequence is invalid")
        if kind == "terminal_claim_relation":
            if assertion.get("result_class") not in {
                None,
                "success",
                "error",
                "mixed",
            }:
                raise ContractError("Traffic terminal result class is invalid")
            relation = assertion.get("relation")
            if relation not in {None, "includes_result", "excludes_result"}:
                raise ContractError("Traffic terminal relation is invalid")
            if relation and (
                not isinstance(assertion.get("result_path"), str)
                or not assertion["result_path"]
            ):
                raise ContractError("Traffic terminal result path is invalid")
            for key in ("required_terms_all", "forbidden_terms"):
                terms = assertion.get(key, [])
                if not isinstance(terms, list) or not all(
                    isinstance(term, str) and term for term in terms
                ):
                    raise ContractError("Traffic terminal terms are invalid")
            if not any(
                key in assertion
                for key in (
                    "result_class",
                    "relation",
                    "required_terms_all",
                    "forbidden_terms",
                )
            ):
                raise ContractError("Traffic terminal relation is empty")
        if kind == "payload_multiplicity":
            source = assertion.get("source")
            if source not in {"input_messages", "tool_result"}:
                raise ContractError("Traffic payload source is invalid")
            if source == "tool_result" and (
                not isinstance(assertion.get("tool_name"), str)
                or not assertion["tool_name"]
                or not isinstance(assertion.get("path"), str)
                or not assertion["path"]
            ):
                raise ContractError("Traffic tool payload assertion is invalid")
            for key in ("minimum", "maximum"):
                if key in assertion and (
                    not isinstance(assertion[key], int)
                    or isinstance(assertion[key], bool)
                    or assertion[key] < 1
                ):
                    raise ContractError("Traffic payload bound is invalid")
            if "minimum" not in assertion:
                raise ContractError("Traffic payload minimum is required")
            if assertion.get("maximum", assertion["minimum"]) < assertion["minimum"]:
                raise ContractError("Traffic payload bounds are invalid")
        if kind == "span_relation" and (
            not isinstance(assertion.get("first_tool"), str)
            or not assertion["first_tool"]
            or not isinstance(assertion.get("second_tool"), str)
            or not assertion["second_tool"]
            or assertion.get("relation") not in {"overlap", "ordered"}
        ):
            raise ContractError("Traffic span relation is invalid")
        assertions.append(assertion)
    return assertions


def _trace_assertion_names(assertions: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(assertion["name"] for assertion in _normalize_trace_assertions(assertions))


def _trace_assertion_result(
    rows: list[dict[str, Any]],
    fixture: dict[str, Any],
) -> tuple[TraceAssertionEvidence, ...]:
    results: list[TraceAssertionEvidence] = []
    request_text = _request_text(fixture["body"])
    for assertion in fixture.get("trace_assertions", []):
        kind = assertion["kind"]
        tool_name = str(assertion.get("tool_name") or "")
        tools = _tool_rows(rows, tool_name) if tool_name else []
        passed = False
        if kind == "tool_call_count":
            passed = len(tools) == assertion["count"]
        elif kind == "tool_argument_presence":
            parsed_arguments = [
                _json_trace_value(row.get("tool_arguments")) for row in tools
            ]
            passed = bool(tools) and all(
                isinstance(arguments, dict)
                and (
                    (assertion["argument"] in arguments)
                    is assertion["present"]
                )
                for arguments in parsed_arguments
            )
        elif kind == "scope_relation":
            scopes = _scope_values(request_text, assertion["scope_kind"])
            selected = (
                scopes[0]
                if scopes and assertion["request_scope"] == "first"
                else scopes[-1] if scopes else None
            )
            comparisons: list[bool] = []
            for row in tools:
                arguments = _json_trace_value(row.get("tool_arguments"))
                result = _json_trace_value(row.get("tool_result"))
                tool_scope = (
                    _nested_value(arguments, assertion["argument"])
                    if isinstance(arguments, dict)
                    else None
                )
                result_scope = (
                    _nested_value(result, assertion["result_field"])
                    if isinstance(result, dict) and assertion.get("result_field")
                    else None
                )
                checks = [
                    isinstance(arguments, dict)
                    and assertion["argument"] in arguments
                    and bool(tool_scope == selected)
                    is assertion["request_tool_equal"]
                ]
                if "request_result_equal" in assertion:
                    checks.append(
                        bool(result_scope == selected)
                        is assertion["request_result_equal"]
                    )
                if "tool_result_equal" in assertion:
                    checks.append(
                        bool(tool_scope == result_scope)
                        is assertion["tool_result_equal"]
                    )
                comparisons.append(all(checks))
            passed = bool(scopes and tools) and all(comparisons)
        elif kind == "tool_result_class":
            passed = bool(tools) and all(
                _result_class(row) == assertion["result_class"] for row in tools
            )
        elif kind == "retry_sequence":
            passed = [
                _result_class(row) for row in tools
            ] == assertion["result_sequence"]
        elif kind == "terminal_claim_relation":
            text = _terminal_text(rows)
            folded = text.casefold()
            classes = {_result_class(row) for row in tools}
            expected_class = assertion.get("result_class")
            class_matches = (
                expected_class is None
                or (expected_class == "mixed" and {"error", "success"} <= classes)
                or (expected_class != "mixed" and classes == {expected_class})
            )
            relation = assertion.get("relation")
            relation_matches = True
            if relation:
                values = [
                    _nested_value(
                        _json_trace_value(row.get("tool_result")),
                        assertion["result_path"],
                    )
                    for row in tools
                ]
                rendered = [
                    str(value).casefold() for value in values if value is not None
                ]
                relation_matches = bool(rendered) and (
                    all(value in folded for value in rendered)
                    if relation == "includes_result"
                    else all(value not in folded for value in rendered)
                )
            passed = (
                bool(text)
                and class_matches
                and relation_matches
                and all(
                    str(term).casefold() in folded
                    for term in assertion.get("required_terms_all", [])
                )
                and all(
                    str(term).casefold() not in folded
                    for term in assertion.get("forbidden_terms", [])
                )
            )
        elif kind == "payload_multiplicity":
            counts: list[int] = []
            if assertion["source"] == "input_messages":
                for row in rows:
                    if row.get("operation_name") != "chat":
                        continue
                    messages = row.get("messages")
                    if not isinstance(messages, list) or not messages:
                        continue
                    parsed = _json_trace_value(messages[0])
                    if not isinstance(parsed, list):
                        continue
                    item_counts = Counter(
                        json.dumps(item, sort_keys=True, ensure_ascii=True)
                        for item in parsed
                    )
                    counts.append(max(item_counts.values(), default=0))
            else:
                for row in tools:
                    value = _nested_value(
                        _json_trace_value(row.get("tool_result")),
                        assertion["path"],
                    )
                    if isinstance(value, int) and not isinstance(value, bool):
                        counts.append(value)
            observed_counts = (
                [max(counts)]
                if counts and assertion["source"] == "input_messages"
                else counts
            )
            passed = bool(observed_counts) and all(
                count >= assertion["minimum"]
                and (
                    "maximum" not in assertion
                    or count <= assertion["maximum"]
                )
                for count in observed_counts
            )
        elif kind == "span_relation":
            first = _tool_rows(rows, assertion["first_tool"])
            second = _tool_rows(rows, assertion["second_tool"])
            pairs = [
                (left_interval, right_interval)
                for left in first
                for right in second
                if (left_interval := _span_interval(left)) is not None
                and (right_interval := _span_interval(right)) is not None
            ]
            if assertion["relation"] == "overlap":
                passed = bool(pairs) and any(
                    left[0] < right[1] and right[0] < left[1]
                    for left, right in pairs
                )
            else:
                passed = bool(pairs) and all(
                    left[1] <= right[0] for left, right in pairs
                )
        elif kind == "operation_sequence":
            observed = [
                str(row.get("operation_name") or "")
                for row in sorted(
                    rows,
                    key=lambda item: str(item.get("timestamp") or ""),
                )
                if row.get("operation_name")
            ]
            position = 0
            for operation in observed:
                if (
                    position < len(assertion["operations"])
                    and operation == assertion["operations"][position]
                ):
                    position += 1
            passed = position == len(assertion["operations"])
        results.append(
            TraceAssertionEvidence(
                assertion=str(assertion["name"]),
                passed=bool(passed),
            )
        )
    return tuple(results)


def _usable_response(response: dict[str, Any], expected_status: int) -> bool:
    if expected_status >= 400:
        return True
    output = response.get("output")
    if not isinstance(output, list) or not output:
        return False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for value in content:
            if not isinstance(value, dict):
                continue
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                return True
    return False


def _response_text(response: dict[str, Any]) -> str:
    values = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                values.append(text.strip())
    return "\n".join(values)


def _semantic_assertion_names(assertions: dict[str, Any]) -> tuple[str, ...]:
    ordered = (
        "response_format",
        "exact_text",
        "json_schema",
        "exact_json_fields",
        "casefold_json_fields",
        "exact_json",
        "required_terms_all",
        "required_terms_any",
        "forbidden_terms",
        "required_claims",
        "forbidden_claims",
        "question_only",
        "minimum_term_occurrences",
        "max_words",
        "min_words",
        "max_characters",
    )
    return tuple(
        name
        for name in ordered
        if name in assertions
        and (
            name == "exact_json"
            or (
                assertions[name] is not None
                and assertions[name] != []
            )
        )
    )


def _semantic_assertion_result(
    response: dict[str, Any],
    fixture: dict[str, Any],
) -> tuple[int, int, tuple[SemanticAssertionEvidence, ...]]:
    assertions = fixture.get("semantic_assertions", {})
    if not assertions:
        return 0, 0, ()
    text = _response_text(response)
    folded = text.casefold()
    results: list[SemanticAssertionEvidence] = []

    def record(assertion: str, passed: bool) -> None:
        results.append(
            SemanticAssertionEvidence(assertion=assertion, passed=bool(passed))
        )

    parsed_json: Any = None
    valid_json = False
    if text:
        try:
            parsed_json = json.loads(text)
            valid_json = True
        except json.JSONDecodeError:
            pass
    response_format = assertions.get("response_format")
    if response_format:
        record(
            "response_format",
            valid_json if response_format == "json" else bool(text) and not valid_json
        )
    exact_text = assertions.get("exact_text")
    if exact_text is not None:
        record("exact_text", text == exact_text)
    json_schema = assertions.get("json_schema")
    if json_schema:
        record(
            "json_schema",
            valid_json
            and not list(Draft202012Validator(json_schema).iter_errors(parsed_json)),
        )
    exact_json_fields = assertions.get("exact_json_fields")
    if exact_json_fields is not None:
        record(
            "exact_json_fields",
            isinstance(parsed_json, dict)
            and all(
                key in parsed_json
                and json_values_equal(parsed_json[key], expected)
                for key, expected in exact_json_fields.items()
            ),
        )
    casefold_json_fields = assertions.get("casefold_json_fields")
    if casefold_json_fields is not None:
        record(
            "casefold_json_fields",
            isinstance(parsed_json, dict)
            and all(
                key in parsed_json
                and isinstance(parsed_json[key], str)
                and parsed_json[key].casefold() == expected.casefold()
                for key, expected in casefold_json_fields.items()
            ),
        )
    if "exact_json" in assertions:
        record(
            "exact_json",
            valid_json
            and json_values_equal(parsed_json, assertions["exact_json"]),
        )
    required_all = assertions.get("required_terms_all", [])
    if required_all:
        record(
            "required_terms_all",
            all(str(term).casefold() in folded for term in required_all),
        )
    required_any = assertions.get("required_terms_any", [])
    if required_any:
        record(
            "required_terms_any",
            any(str(term).casefold() in folded for term in required_any),
        )
    forbidden = assertions.get("forbidden_terms", [])
    if forbidden:
        record(
            "forbidden_terms",
            all(str(term).casefold() not in folded for term in forbidden),
        )
    required_claims = assertions.get("required_claims", [])
    if required_claims:
        record(
            "required_claims",
            all(str(claim).casefold() in folded for claim in required_claims),
        )
    forbidden_claims = assertions.get("forbidden_claims", [])
    if forbidden_claims:
        record(
            "forbidden_claims",
            all(str(claim).casefold() not in folded for claim in forbidden_claims),
        )
    if assertions.get("question_only") is True:
        sentences = re.findall(r"[^.!?]+[.!?]", text)
        record(
            "question_only",
            bool(sentences)
            and text.rstrip().endswith("?")
            and all(sentence.rstrip().endswith("?") for sentence in sentences),
        )
    minimum_occurrences = assertions.get("minimum_term_occurrences")
    if minimum_occurrences is not None:
        record(
            "minimum_term_occurrences",
            bool(text)
            and all(
                len(
                    re.findall(
                        rf"(?<!\w){re.escape(str(term).casefold())}(?!\w)",
                        folded,
                    )
                )
                >= int(minimum)
                for term, minimum in minimum_occurrences.items()
            ),
        )
    max_words = assertions.get("max_words")
    if max_words is not None:
        record(
            "max_words",
            bool(text) and len(re.findall(r"\S+", text)) <= int(max_words),
        )
    min_words = assertions.get("min_words")
    if min_words is not None:
        record(
            "min_words",
            bool(text) and len(re.findall(r"\S+", text)) >= int(min_words),
        )
    max_characters = assertions.get("max_characters")
    if max_characters is not None:
        record("max_characters", bool(text) and len(text) <= int(max_characters))
    return (
        len(results),
        sum(item.passed for item in results),
        tuple(results),
    )


def _normalize_fixture(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("Traffic request must be an object")
    request = value.get("request")
    body = request.get("body") if isinstance(request, dict) else None
    if not isinstance(body, dict) or "input" not in body:
        raise ContractError("Traffic request must contain a Responses request body")
    if "tool_fixtures" in value:
        raise ContractError("Endpoint traffic cannot contain tool fixtures")
    expected = value.get("expected")
    expected_status = (
        int(expected.get("http_status", 200)) if isinstance(expected, dict) else 200
    )
    semantic_assertions = (
        expected.get("semantic_assertions", {})
        if isinstance(expected, dict)
        else {}
    )
    trace_assertions = _normalize_trace_assertions(
        expected.get("trace_assertions") if isinstance(expected, dict) else None
    )
    if not isinstance(semantic_assertions, dict) or any(
        key
        not in {
            "response_format",
            "exact_text",
            "required_terms_all",
            "required_terms_any",
            "forbidden_terms",
            "max_words",
            "max_characters",
            "json_schema",
            "exact_json_fields",
            "casefold_json_fields",
            "exact_json",
            "required_claims",
            "forbidden_claims",
            "question_only",
            "minimum_term_occurrences",
            "min_words",
        }
        for key in semantic_assertions
    ):
        raise ContractError("Traffic semantic assertions are invalid")
    for key in (
        "required_terms_all",
        "required_terms_any",
        "forbidden_terms",
        "required_claims",
        "forbidden_claims",
    ):
        terms = semantic_assertions.get(key, [])
        if not isinstance(terms, list) or not all(
            isinstance(term, str) and term for term in terms
        ):
            raise ContractError("Traffic semantic assertion terms are invalid")
    for key in ("min_words", "max_words", "max_characters"):
        bound = semantic_assertions.get(key)
        if bound is not None and (
            not isinstance(bound, int) or isinstance(bound, bool) or bound < 1
        ):
            raise ContractError("Traffic semantic assertion bound is invalid")
    question_only = semantic_assertions.get("question_only")
    if question_only is not None and question_only is not True:
        raise ContractError("Traffic question-only assertion is invalid")
    minimum_occurrences = semantic_assertions.get("minimum_term_occurrences")
    if minimum_occurrences is not None and (
        not isinstance(minimum_occurrences, dict)
        or not minimum_occurrences
        or any(
            not isinstance(term, str)
            or not term
            or not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or minimum < 2
            for term, minimum in minimum_occurrences.items()
        )
    ):
        raise ContractError("Traffic term-occurrence assertions are invalid")
    json_schema = semantic_assertions.get("json_schema")
    if json_schema is not None:
        if not isinstance(json_schema, dict) or not json_schema:
            raise ContractError("Traffic semantic assertion JSON schema is invalid")
        try:
            Draft202012Validator.check_schema(json_schema)
        except SchemaError as error:
            raise ContractError(
                "Traffic semantic assertion JSON schema is invalid"
            ) from error
    exact_json_fields = semantic_assertions.get("exact_json_fields")
    if exact_json_fields is not None and (
        not isinstance(exact_json_fields, dict)
        or not exact_json_fields
        or not all(isinstance(key, str) and key for key in exact_json_fields)
    ):
        raise ContractError("Traffic exact JSON field assertions are invalid")
    casefold_json_fields = semantic_assertions.get("casefold_json_fields")
    if casefold_json_fields is not None and (
        not isinstance(casefold_json_fields, dict)
        or not casefold_json_fields
        or not all(
            isinstance(key, str)
            and key
            and isinstance(expected, str)
            for key, expected in casefold_json_fields.items()
        )
    ):
        raise ContractError("Traffic casefold JSON field assertions are invalid")
    activation_gate = (
        expected.get("activation_gate", False)
        if isinstance(expected, dict)
        else False
    )
    if not isinstance(activation_gate, bool):
        raise ContractError("Traffic activation gate must be a boolean")
    conversation = body.get("conversation")
    conversation_key = (
        str(conversation.get("id") or "")
        if isinstance(conversation, dict)
        else str(conversation or "")
    )
    if not conversation_key:
        conversation_key = str(value.get("id") or uuid.uuid4())
    return {
        "id": str(value.get("id") or ""),
        "body": dict(body),
        "expected_status": expected_status,
        "semantic_assertions": semantic_assertions,
        "trace_assertions": trace_assertions,
        "activation_gate": activation_gate,
        "conversation_key": conversation_key,
    }


def _complete_operation_ids(
    tables: Any,
    expected_references: tuple[str, ...],
) -> tuple[str, ...] | None:
    operations_by_reference: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        for row in table.rows:
            operation_id = str(row[0]).lower()
            if not _TRACE_ID.fullmatch(operation_id):
                continue
            references = row[1] if len(row) > 1 else []
            if isinstance(references, str):
                try:
                    references = json.loads(references)
                except json.JSONDecodeError:
                    references = [references]
            if isinstance(references, list):
                for reference in references:
                    if str(reference) in expected_references:
                        operations_by_reference[str(reference)].add(operation_id)
    ordered: list[str] = []
    for reference in expected_references:
        matched = operations_by_reference.get(reference, set())
        if len(matched) != 1:
            return None
        ordered.append(next(iter(matched)))
    if len(set(ordered)) != len(ordered):
        return None
    return tuple(ordered)


def _telemetry_string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = [value]
    else:
        decoded = value
    if not isinstance(decoded, list):
        return set()
    return {str(item) for item in decoded if str(item)}


def _operation_correlation_impossible(
    tables: Any,
    expected_references: tuple[str, ...],
) -> bool:
    expected = set(expected_references)
    operations_by_reference: dict[str, set[str]] = defaultdict(set)
    references_by_operation: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        for row in table.rows:
            operation_id = str(row[0]).lower()
            if not _TRACE_ID.fullmatch(operation_id):
                continue
            references = row[1] if len(row) > 1 else []
            if isinstance(references, str):
                try:
                    references = json.loads(references)
                except json.JSONDecodeError:
                    references = [references]
            if not isinstance(references, list):
                continue
            for value in references:
                reference = str(value)
                if reference not in expected:
                    continue
                operations_by_reference[reference].add(operation_id)
                references_by_operation[operation_id].add(reference)
    return any(len(values) > 1 for values in operations_by_reference.values()) or any(
        len(values) > 1 for values in references_by_operation.values()
    )


def _validate_response_references(
    response_references: tuple[str, ...],
    expected_count: int,
) -> None:
    if (
        expected_count < 1
        or len(response_references) != expected_count
        or any(
            not isinstance(value, str)
            or _RESPONSE_REFERENCE.fullmatch(value) is None
            for value in response_references
        )
        or len(set(response_references)) != expected_count
    ):
        raise ContractError(
            "Endpoint response references must be nonempty, unique, and well formed"
        )


def _validate_operation_references(
    operation_ids: tuple[str, ...],
    expected_count: int,
) -> None:
    if (
        expected_count < 1
        or len(operation_ids) != expected_count
        or any(
            not isinstance(value, str) or _TRACE_ID.fullmatch(value) is None
            for value in operation_ids
        )
        or len(set(operation_ids)) != expected_count
    ):
        raise ContractError(
            "Trace assertion operations must uniquely cover every endpoint request"
        )
