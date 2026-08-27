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
from typing import Any, Callable

from agent_insights_quality.models import (
    InsightEvidence,
    InsightRunEvidence,
    InvocationEvidence,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError
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
_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
_LOGS_SCOPE = "https://api.loganalytics.io/.default"
_TRANSIENT_HTTP = {408, 424, 429, 500, 502, 503, 504}


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
    ) -> None:
        self._profile = profile
        self._raw_token_provider = token_provider or _azure_cli_token
        self._sleep = sleep
        self._token_lock = threading.Lock()
        self._token_cache: dict[str, tuple[float, str]] = {}
        self._telemetry_query_lock = threading.Lock()
        self._logs_client_instance: Any | None = None
        self._progress_lock = threading.Lock()
        self._progress_started = time.monotonic()

    def report_progress(self, message: str) -> None:
        elapsed = time.monotonic() - self._progress_started
        with self._progress_lock:
            print(f"[aiq +{elapsed:07.1f}s] {message}", flush=True)

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

    def assert_clean_window(self, agent_name: str, lookback_hours: int) -> None:
        try:
            from azure.monitor.query import LogsQueryStatus
        except ImportError as error:
            raise ContractError(
                'Clean-window preflight requires installation with ".[azure]"'
            ) from error
        query = f"""
union traces, dependencies, requests
| where timestamp >= ago({lookback_hours}h)
| extend operation_name = tostring(customDimensions["gen_ai.operation.name"])
| extend observed_agent = tostring(customDimensions["gen_ai.agent.name"])
| where operation_name == "invoke_agent" and observed_agent == "{agent_name}"
| summarize operation_count=dcount(operation_Id)
"""
        result = self._query_resource(
            self._logs_client(),
            query,
            timespan=timedelta(hours=lookback_hours),
        )
        if result.status != LogsQueryStatus.SUCCESS or not result.tables:
            raise ContractError("Clean-window preflight telemetry query failed")
        count = int(result.tables[0].rows[0][0]) if result.tables[0].rows else 0
        if count:
            raise ContractError(
                f"{agent_name} has pre-existing traces in the minimum lookback window"
            )

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
        started = datetime.now(UTC)
        response_references: list[str] = []
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
            completed_groups: dict[
                str,
                list[tuple[int, list[str], bool, int, int]],
            ] = {}
            for future in as_completed(futures):
                completed_groups[futures[future]] = future.result()
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
        for _, references, usable, assertion_count, assertions_passed in ordered:
            response_references.extend(references)
            usable_response_count += int(usable)
            semantic_assertion_count += assertion_count
            semantic_assertions_passed += assertions_passed
        completed = datetime.now(UTC)
        return InvocationEvidence(
            operation_ids=(),
            response_references=tuple(response_references),
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            request_count=len(requests),
            allow_window_correlation=agent_type != "prompt",
            response_count=len(ordered),
            usable_response_count=usable_response_count,
            semantic_assertion_count=semantic_assertion_count,
            semantic_assertions_passed=semantic_assertions_passed,
        )

    def _invoke_group(
        self,
        agent_name: str,
        agent_type: str,
        foundry_version: str,
        fixtures: list[dict[str, Any]],
        seed: int,
    ) -> list[tuple[int, list[str], bool, int, int]]:
        if agent_type == "prompt":
            results: list[tuple[int, list[str], bool, int, int]] = []
            previous_response_id: str | None = None
            for fixture in fixtures:
                response_ids, usable, assertion_count, assertions_passed = (
                    self._invoke_prompt(
                    agent_name,
                    foundry_version,
                    fixture,
                    seed + int(fixture["_index"]),
                    previous_response_id,
                    )
                )
                previous_response_id = response_ids[-1]
                results.append(
                    (
                        int(fixture["_index"]),
                        response_ids,
                        usable,
                        assertion_count,
                        assertions_passed,
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
    ) -> tuple[list[str], bool, int, int]:
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
        body["metadata"] = {
            **body.get("metadata", {}),
            "traffic_seed": str(seed),
        }
        response = self._json_request(
            "POST",
            f"{self._profile.project_endpoint}/openai/v1/responses",
            body,
            expected={fixture["expected_status"]},
            retry_statuses=_TRANSIENT_HTTP,
            retry_no_response=True,
        )
        response_ids: list[str] = []
        for _ in range(8):
            response_id = str(response.get("id") or "")
            if not response_id or response_id in response_ids:
                raise ContractError("Prompt response identity is missing or repeated")
            response_ids.append(response_id)
            calls = [
                value
                for value in response.get("output", [])
                if isinstance(value, dict) and value.get("type") == "function_call"
            ]
            if not calls:
                assertion_count, assertions_passed = _semantic_assertion_result(
                    response,
                    fixture,
                )
                return (
                    response_ids,
                    _usable_response(response, fixture["expected_status"]),
                    assertion_count,
                    assertions_passed,
                )
            outputs = []
            tool_outputs = fixture.get("tool_outputs", {})
            for call in calls:
                name = str(call.get("name") or "")
                call_id = str(call.get("call_id") or "")
                configured = tool_outputs.get(name)
                if not call_id:
                    raise ContractError("Prompt returned a tool call without an identity")
                raw_arguments = str(call.get("arguments") or "")
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as error:
                    raise ContractError("Prompt emitted invalid tool arguments") from error
                if not configured:
                    result = {
                        "error": {
                            "code": "unexpected_tool_call",
                            "tool": name,
                        }
                    }
                else:
                    matching = next(
                        (
                            value
                            for value in configured
                            if _arguments_match(arguments, value["arguments"])
                        ),
                        configured[0] if len(configured) == 1 else None,
                    )
                    if matching is None:
                        result = {
                            "error": {
                                "code": "synthetic_argument_fixture_mismatch",
                                "tool": name,
                            }
                        }
                    else:
                        results = matching["results"]
                        result = results.pop(0) if len(results) > 1 else results[0]
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, sort_keys=True),
                    }
                )
            response = self._json_request(
                "POST",
                f"{self._profile.project_endpoint}/openai/v1/responses",
                {
                    "input": outputs,
                    "previous_response_id": response_id,
                    "store": True,
                    "agent_reference": reference,
                },
                retry_statuses=_TRANSIENT_HTTP,
                retry_no_response=True,
            )
        raise ContractError("Prompt exceeded the bounded tool-turn limit")

    def _create_hosted_session(
        self,
        agent_name: str,
        foundry_version: str,
    ) -> str:
        session = self._json_request(
            "POST",
            f"{self._profile.project_endpoint}/agents/"
            f"{urllib.parse.quote(agent_name, safe='')}/endpoint/sessions",
            {
                "version_indicator": {
                    "type": "version_ref",
                    "agent_version": foundry_version,
                }
            },
            hosted=True,
            retry_statuses=_TRANSIENT_HTTP,
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
            raise ContractError("Hosted session did not bind to the exact version")
        return session_id

    def _invoke_hosted(
        self,
        agent_name: str,
        session_id: str,
        fixture: dict[str, Any],
        seed: int,
    ) -> tuple[list[str], bool, int, int]:
        del seed
        body = {
            "input": fixture["body"]["input"],
            "agent_session_id": session_id,
            "store": False,
        }
        correlation_id = str(uuid.uuid4())
        response = self._json_request(
            "POST",
            f"{self._profile.project_endpoint}/agents/"
            f"{urllib.parse.quote(agent_name, safe='')}"
            "/endpoint/protocols/openai/responses",
            body,
            hosted=True,
            expected={fixture["expected_status"]},
            correlation_id=correlation_id,
            retry_statuses=_TRANSIENT_HTTP,
            retry_no_response=True,
        )
        request_reference = str(response.get("_request_reference") or "")
        if not request_reference:
            raise ContractError("Hosted response omitted its request reference")
        assertion_count, assertions_passed = _semantic_assertion_result(
            response,
            fixture,
        )
        return (
            [request_reference],
            _usable_response(response, fixture["expected_status"]),
            assertion_count,
            assertions_passed,
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
        escaped = ", ".join(
            f'"{value.replace(chr(34), chr(92) + chr(34))}"'
            for value in invocation.response_references
        )
        query = f"""
union traces, dependencies, requests
| where timestamp >= datetime({start.astimezone(UTC).isoformat()})
| extend response_id = tostring(customDimensions["gen_ai.response.id"])
| extend request_id = coalesce(
    tostring(customDimensions["x-ms-client-request-id"]),
    tostring(customDimensions["client_request_id"]),
    tostring(customDimensions["request_id"]))
| extend agent_version = tostring(customDimensions["gen_ai.agent.version"])
| extend matched_reference = iff(response_id in ({escaped}), response_id, request_id)
| where matched_reference in ({escaped}) and agent_version == "{foundry_version}"
| summarize matched_references=make_set(matched_reference) by operation_Id
"""
        deadline = time.monotonic() + 15 * 60
        next_progress = time.monotonic() + 60
        window_query = f"""
union traces, dependencies, requests
| where timestamp >= datetime({start.astimezone(UTC).isoformat()})
  and timestamp <= datetime({traffic_end.astimezone(UTC).isoformat()})
| extend operation_name = tostring(customDimensions["gen_ai.operation.name"])
| extend observed_agent = tostring(customDimensions["gen_ai.agent.name"])
| extend agent_version = tostring(customDimensions["gen_ai.agent.version"])
| where operation_name == "invoke_agent"
  and observed_agent == "{agent_name}"
  and agent_version == "{foundry_version}"
| summarize by operation_Id
"""
        while time.monotonic() < deadline:
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
            if invocation.allow_window_correlation:
                window_result = self._query_resource(
                    client,
                    window_query,
                    timespan=(start, query_end),
                )
                if window_result.status == LogsQueryStatus.SUCCESS:
                    operations = tuple(
                        sorted(
                            {
                                str(row[0]).lower()
                                for table in window_result.tables
                                for row in table.rows
                                if _TRACE_ID.fullmatch(str(row[0]).lower())
                            }
                        )
                    )
                    if len(operations) == invocation.request_count:
                        return operations
            if time.monotonic() >= next_progress:
                elapsed = int(15 * 60 - max(deadline - time.monotonic(), 0))
                self.report_progress(
                    f"{agent_name}/{foundry_version}: waiting for telemetry "
                    f"({elapsed}s)"
                )
                next_progress = time.monotonic() + 60
            self._sleep(15)
        raise ContractError("Natural telemetry did not arrive before the bounded deadline")

    def run_insights(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: int,
    ) -> InsightRunEvidence:
        last_result: InsightRunEvidence | None = None
        for attempt in range(3):
            if attempt:
                self.report_progress(
                    f"{agent_name}/{foundry_version}: retrying Agent Insights run "
                    f"({attempt + 1}/3)"
                )
            last_result = self._run_insights_once(
                agent_name=agent_name,
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                lookback_hours=lookback_hours,
            )
            if last_result.status.lower() == "succeeded":
                return last_result
            if attempt < 2:
                self._sleep(30)
        if last_result is None:
            raise ContractError("Agent Insights run retry loop did not execute")
        return last_result

    def _run_insights_once(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: int,
    ) -> InsightRunEvidence:
        before = self._insight_revisions(monitor_id)
        run = self._json_request(
            "POST",
            self._insights_url(
                f"/agent_insight_monitors/{urllib.parse.quote(monitor_id, safe='')}/runs"
            ),
            {"lookback_hours": lookback_hours},
            expected={200, 201, 202},
            retry_statuses={409, *_TRANSIENT_HTTP},
        )
        run_id = str(run.get("id") or "")
        if not run_id:
            raise ContractError("Agent Insights run omitted its identity")
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
        after = self._list_insights(monitor_id)
        changed = [
            item
            for item in after
            if before.get(str(item.get("id") or ""))
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
            and set(self._linked_ids(value)).intersection(operation_ids)
        )
        return InsightRunEvidence(
            run_reference=_opaque(run_id),
            window_start=str(run.get("window_start") or run.get("windowStart") or ""),
            window_end=str(run.get("window_end") or run.get("windowEnd") or ""),
            status=str(run.get("status") or ""),
            insights=evidence,
        )

    def verify_trace_contract(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        required_operations: tuple[str, ...],
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
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            result = self._query_resource(
                self._logs_client(),
                query,
                timespan=timedelta(hours=3),
            )
            if (
                result.status == LogsQueryStatus.SUCCESS
                and result.tables
                and _trace_contract_ready(
                    result.tables,
                    operation_ids,
                    required_operations,
                )
            ):
                return
            self._sleep(15)
        raise ContractError("Trace contract did not stabilize before the bounded deadline")

    def trace_behavior_evidence(
        self,
        operation_ids: tuple[str, ...],
    ) -> dict[str, Any]:
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
        query = f"""
union traces, dependencies, requests
| where operation_Id in ({values})
| extend operation_name=tostring(customDimensions["gen_ai.operation.name"])
| extend tool_name=coalesce(
    tostring(customDimensions["gen_ai.tool.name"]),
    tostring(customDimensions["tool.name"]))
| extend tool_call_id=coalesce(
    tostring(customDimensions["gen_ai.tool.call.id"]),
    tostring(customDimensions["tool.call.id"]))
| extend error_type=tostring(customDimensions["error.type"])
| extend tool_ok=tostring(customDimensions["tool.ok"])
| extend tool_result=tostring(customDimensions["gen_ai.tool.call.result"])
| extend input_messages=tostring(customDimensions["gen_ai.input.messages"])
| extend output_messages=tostring(customDimensions["gen_ai.output.messages"])
| project operation_Id, operation_name, tool_name, tool_call_id, error_type, tool_ok, tool_result,
    input_messages, output_messages, timestamp
"""
        result = self._query_resource(
            self._logs_client(),
            query,
            timespan=timedelta(days=90),
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
                "messages": [str(row[7] or ""), str(row[8] or "")],
                "timestamp": str(row[9] or ""),
            }
            for table in result.tables
            for row in table.rows
        ]
        return _trace_behavior_summary(rows)

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
        while attempt < max_attempts:
            request = urllib.request.Request(
                url,
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    status = response.status
                    payload = response.read()
            except urllib.error.HTTPError as error:
                status = error.code
                payload = error.read()
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
                raise ContractError(
                    "Remote operation failed before a response was received"
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
        allowed = expected or {200, 201, 202}
        if status not in allowed:
            code, message = _remote_error(payload)
            raise ContractError(
                f"Remote operation failed with HTTP {status}"
                + (
                    f" ({code or 'remote_error'}: {message})"
                    if code or message
                    else ""
                )
            )
        if not payload:
            value: Any = {}
        else:
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                value = {}
        if not isinstance(value, dict):
            raise ContractError("Remote operation returned an invalid JSON shape")
        value["_http_status"] = status
        value["_request_reference"] = request_reference
        return value


def _azure_cli_token(scope: str) -> str:
    for attempt in range(5):
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


def _trace_contract_ready(
    tables: list[Any],
    operation_ids: tuple[str, ...],
    required_operations: tuple[str, ...],
) -> bool:
    seen_ids: set[str] = set()
    observed_operations: set[str] = set()
    for table in tables:
        for row in table.rows:
            operation_id = str(row[0]).lower()
            if not _TRACE_ID.fullmatch(operation_id):
                continue
            seen_ids.add(operation_id)
            operations = row[1]
            if isinstance(operations, str):
                operations = json.loads(operations)
            if isinstance(operations, list):
                observed_operations.update(
                    str(value) for value in operations if value
                )
            if int(row[2]) < 1 or int(row[3]) < 1:
                return False
    return (
        seen_ids == set(operation_ids)
        and not (set(required_operations) - observed_operations)
    )


def _trace_behavior_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    call_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    anonymous_calls: Counter[str] = Counter()
    response_ids: set[str] = set()
    successful_responses: set[str] = set()
    error_codes: Counter[str] = Counter()
    recorded_errors: set[tuple[str, str]] = set()
    assistant_response_operations: set[str] = set()
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
    return {
        "operation_count": len({row["operation_id"] for row in rows}),
        "tool_call_counts": dict(sorted(call_counts.items())),
        "tool_response_count": len(response_ids),
        "successful_tool_response_count": len(successful_responses),
        "error_codes": dict(sorted(error_codes.items())),
        "assistant_response_count": len(assistant_response_operations),
    }


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


def _semantic_assertion_result(
    response: dict[str, Any],
    fixture: dict[str, Any],
) -> tuple[int, int]:
    assertions = fixture.get("semantic_assertions", {})
    if not assertions:
        return 0, 0
    text = _response_text(response)
    folded = text.casefold()
    results = []
    response_format = assertions.get("response_format")
    if response_format:
        valid_json = False
        if text:
            try:
                json.loads(text)
                valid_json = True
            except json.JSONDecodeError:
                pass
        results.append(
            valid_json if response_format == "json" else bool(text) and not valid_json
        )
    required_all = assertions.get("required_terms_all", [])
    if required_all:
        results.append(all(str(term).casefold() in folded for term in required_all))
    required_any = assertions.get("required_terms_any", [])
    if required_any:
        results.append(any(str(term).casefold() in folded for term in required_any))
    forbidden = assertions.get("forbidden_terms", [])
    if forbidden:
        results.append(
            all(str(term).casefold() not in folded for term in forbidden)
        )
    return len(results), sum(results)


def _normalize_fixture(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("Traffic request must be an object")
    request = value.get("request")
    body = request.get("body") if isinstance(request, dict) else None
    if not isinstance(body, dict) or "input" not in body:
        raise ContractError("Traffic request must contain a Responses request body")
    fixtures: dict[str, list[dict[str, Any]]] = {}
    for item in value.get("tool_fixtures", []):
        if not isinstance(item, dict) or not item.get("tool"):
            raise ContractError("Tool fixture is invalid")
        if "sequence" in item:
            sequence = item["sequence"]
            if not isinstance(sequence, list) or not sequence:
                raise ContractError("Tool fixture return sequence is invalid")
            results = [
                value.get("returns") if isinstance(value, dict) and "returns" in value else value
                for value in sequence
            ]
        elif "returns" in item:
            results = [item["returns"]]
        else:
            raise ContractError("Tool fixture has no synthetic result")
        fixtures.setdefault(str(item["tool"]), []).append(
            {
                "arguments": item.get("arguments", {}),
                "results": results,
            }
        )
    expected = value.get("expected")
    expected_status = (
        int(expected.get("http_status", 200)) if isinstance(expected, dict) else 200
    )
    semantic_assertions = (
        expected.get("semantic_assertions", {})
        if isinstance(expected, dict)
        else {}
    )
    if not isinstance(semantic_assertions, dict) or any(
        key
        not in {
            "response_format",
            "required_terms_all",
            "required_terms_any",
            "forbidden_terms",
        }
        for key in semantic_assertions
    ):
        raise ContractError("Traffic semantic assertions are invalid")
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
        "tool_outputs": fixtures,
        "expected_status": expected_status,
        "semantic_assertions": semantic_assertions,
        "conversation_key": conversation_key,
    }


def _complete_operation_ids(
    tables: Any,
    expected_references: tuple[str, ...],
) -> tuple[str, ...] | None:
    operations: set[str] = set()
    seen: set[str] = set()
    for table in tables:
        for row in table.rows:
            operation_id = str(row[0]).lower()
            if not _TRACE_ID.fullmatch(operation_id):
                continue
            operations.add(operation_id)
            references = row[1] if len(row) > 1 else []
            if isinstance(references, str):
                try:
                    references = json.loads(references)
                except json.JSONDecodeError:
                    references = [references]
            if isinstance(references, list):
                seen.update(str(value) for value in references)
    if not set(expected_references).issubset(seen):
        return None
    return tuple(sorted(operations))


def _arguments_match(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    if set(actual) != set(expected):
        return False
    for key, expected_value in expected.items():
        actual_value = actual[key]
        if key == "location" and isinstance(actual_value, str) and isinstance(
            expected_value, str
        ):
            normalized_actual = actual_value.casefold().strip()
            normalized_expected = expected_value.casefold().strip()
            if normalized_actual == normalized_expected or normalized_actual.startswith(
                normalized_expected + ","
            ):
                continue
        if actual_value != expected_value:
            return False
    return True
