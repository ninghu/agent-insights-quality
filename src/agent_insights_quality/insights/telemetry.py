from __future__ import annotations

import importlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from agent_insights_quality.runtime.errors import RuntimeFailure

_W3C_TRACE_ID = re.compile(r"^(?!0{32})[0-9a-f]{32}$")


def _kql(value: str) -> str:
    return value.replace("'", "''")


@dataclass(frozen=True, slots=True)
class TelemetryExpectation:
    invocation_id: str | None
    response_id: str | None
    session_id: str | None
    model_deployment: str
    required_operations: frozenset[str] = frozenset({"invoke_agent", "chat"})

    def identifiers(self) -> set[str]:
        return {value for value in (self.invocation_id, self.response_id, self.session_id) if value}


@dataclass(frozen=True, slots=True)
class TraceCorrelation:
    operation_id: str
    span_count: int
    root_count: int
    span_ids: tuple[str, ...] = ()
    observed_at: datetime | None = None


class TelemetryQuery(Protocol):
    def query(
        self,
        resource_id: str,
        query: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[Mapping[str, Any]]: ...


class AzureTelemetryQuery:
    """Read-only adapter over Azure Monitor query APIs."""

    def __init__(self, credential: Any) -> None:
        query_module = importlib.import_module("azure.monitor.query")
        self._client = query_module.LogsQueryClient(credential=credential, retry_total=0)

    def query(
        self,
        resource_id: str,
        query: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[Mapping[str, Any]]:
        result = self._client.query_resource(
            resource_id,
            query,
            timespan=(start, end),
            server_timeout=60,
        )
        status = str(getattr(getattr(result, "status", ""), "value", getattr(result, "status", "")))
        if status.casefold() != "success":
            raise RuntimeFailure(
                "telemetry_query_failed",
                "Application Insights read-only query did not succeed.",
                transient=True,
            )
        tables = list(getattr(result, "tables", []) or [])
        if len(tables) != 1:
            raise RuntimeFailure("invalid_telemetry_result", "Telemetry query returned an invalid table set.")
        columns = [str(getattr(column, "name", column)) for column in tables[0].columns]
        return [dict(zip(columns, row, strict=True)) for row in tables[0].rows]


def correlation_query(
    *,
    agent: str,
    version: str,
    expectations: Sequence[TelemetryExpectation],
) -> str:
    identifiers = sorted({identifier for item in expectations for identifier in item.identifiers()})
    if not identifiers:
        raise RuntimeFailure("missing_correlation_identifier", "At least one correlation identifier is required.")
    literals = ", ".join(f"'{_kql(identifier)}'" for identifier in identifiers)
    return f"""
let roots = materialize(
    union isfuzzy=true requests, dependencies
    | extend operation_id=tolower(tostring(operation_Id))
    | extend agent_name=tostring(customDimensions["gen_ai.agent.name"])
    | extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
    | extend invocation_id=tostring(customDimensions["gen_ai.invocation.id"])
    | extend response_id=tostring(customDimensions["gen_ai.response.id"])
    | extend hosted_response_id=tostring(customDimensions["azure.ai.agentserver.response_id"])
    | extend session_id=tostring(customDimensions["azure.ai.agentserver.session_id"])
    | where agent_name == '{_kql(agent)}' and agent_version == '{_kql(version)}'
    | where invocation_id in ({literals}) or response_id in ({literals})
        or hosted_response_id in ({literals}) or session_id in ({literals})
    | summarize invocation_ids=make_set(invocation_id, 100),
        response_ids=make_set(response_id, 100),
        hosted_response_ids=make_set(hosted_response_id, 100),
        session_ids=make_set(session_id, 100)
      by operation_id, agent_name, agent_version
);
union isfuzzy=true requests, dependencies
| extend operation_id=tolower(tostring(operation_Id))
| extend span_id=tostring(id), parent_id=tostring(operation_ParentId)
| extend span_name=tostring(coalesce(customDimensions["gen_ai.operation.name"], name))
| extend span_agent_name=tostring(customDimensions["gen_ai.agent.name"])
| extend span_agent_version=tostring(customDimensions["gen_ai.agent.version"])
| extend span_model=tostring(coalesce(
    customDimensions["gen_ai.request.model"],
    customDimensions["gen_ai.response.model"]
  ))
| where operation_id in (roots | project operation_id)
| join kind=inner roots on operation_id
| project timestamp, operation_id, span_id, parent_id, span_name, span_agent_name,
    span_agent_version, span_model, agent_name,
    agent_version, invocation_id=invocation_ids, response_id=response_ids,
    hosted_response_id=hosted_response_ids, session_id=session_ids
""".strip()


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {value} if value else set()
        value = decoded
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return set()
    return {str(item) for item in value if item}


def _row_identifiers(row: Mapping[str, Any], keys: Sequence[str]) -> set[str]:
    result: set[str] = set()
    for key in keys:
        value = row.get(key)
        result |= _strings(value) if isinstance(value, (list, tuple, str)) else ({str(value)} if value else set())
    return result


def correlate_complete_traces(
    rows: Sequence[Mapping[str, Any]],
    expectations: Sequence[TelemetryExpectation],
    *,
    agent: str,
    version: str,
    start: datetime,
    end: datetime,
) -> list[TraceCorrelation] | None:
    operations: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if (
            row.get("agent_name") != agent
            or row.get("agent_version") != version
        ):
            raise RuntimeFailure(
                "telemetry_provenance_mismatch",
                "Telemetry did not match the exact agent and version selected in the telemetry resource.",
            )
        operation_id = str(row.get("operation_id") or "").casefold()
        if not _W3C_TRACE_ID.fullmatch(operation_id):
            raise RuntimeFailure("invalid_operation_id", "Telemetry operation_Id was not a W3C trace ID.")
        timestamp = row.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or not (start <= timestamp < end):
            raise RuntimeFailure("telemetry_window_mismatch", "Telemetry fell outside the exact half-open window.")
        operations.setdefault(operation_id, []).append(row)

    matched: list[TraceCorrelation] = []
    used: set[str] = set()
    for expectation in expectations:
        primary = {
            value for value in (expectation.invocation_id, expectation.response_id) if value
        }
        identifiers = primary or ({expectation.session_id} if expectation.session_id else set())
        keys = (
            ("invocation_id", "response_id", "hosted_response_id")
            if primary
            else ("session_id",)
        )
        candidates = {
            operation_id
            for operation_id, spans in operations.items()
            if any(identifiers & _row_identifiers(span, keys) for span in spans)
        }
        if not candidates:
            return None
        if len(candidates) != 1:
            raise RuntimeFailure(
                "ambiguous_telemetry_correlation",
                "A runtime identifier correlated to multiple operation IDs.",
            )
        operation_id = next(iter(candidates))
        if operation_id in used:
            raise RuntimeFailure(
                "duplicate_telemetry_correlation",
                "Multiple invocations correlated to the same operation ID.",
            )
        spans = operations[operation_id]
        span_id_list = [str(row.get("span_id") or "") for row in spans if row.get("span_id")]
        span_ids = set(span_id_list)
        parents = [str(row.get("parent_id") or "") for row in spans]
        roots = sum(not parent for parent in parents)
        root_ids = {
            str(row.get("span_id") or "")
            for row in spans
            if row.get("span_id") and not row.get("parent_id")
        }
        reachable = set(root_ids)
        while True:
            children = {
                str(row.get("span_id") or "")
                for row in spans
                if row.get("span_id") and str(row.get("parent_id") or "") in reachable
            }
            expanded = reachable | children
            if expanded == reachable:
                break
            reachable = expanded
        operations_present = {str(row.get("span_name") or "") for row in spans}
        required_spans = [
            row for row in spans if str(row.get("span_name") or "") in expectation.required_operations
        ]
        required_provenance_valid = all(
            row.get("span_agent_name") == agent and row.get("span_agent_version") == version
            for row in required_spans
        )
        chat_spans = [row for row in required_spans if row.get("span_name") == "chat"]
        model_valid = bool(chat_spans) and all(
            row.get("span_model") == expectation.model_deployment for row in chat_spans
        )
        if (
            roots != 1
            or len(span_id_list) != len(spans)
            or len(span_ids) != len(span_id_list)
            or any(parent and parent not in span_ids for parent in parents)
            or reachable != span_ids
            or not expectation.required_operations.issubset(operations_present)
            or not required_provenance_valid
            or not model_valid
        ):
            return None
        used.add(operation_id)
        observed = min(
            (
                datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
                if isinstance(row.get("timestamp"), str)
                else row["timestamp"]
            )
            for row in spans
        )
        matched.append(
            TraceCorrelation(
                operation_id,
                len(spans),
                roots,
                tuple(sorted(span_ids)),
                observed.astimezone(UTC),
            )
        )
    return matched


def wait_for_correlated_traces(
    query_client: TelemetryQuery,
    *,
    resource_id: str,
    agent: str,
    version: str,
    expectations: Sequence[TelemetryExpectation],
    start: datetime,
    end: datetime,
    timeout_seconds: float = 900,
    poll_seconds: float = 20,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[TraceCorrelation]:
    if timeout_seconds <= 0 or start.tzinfo is None or end.tzinfo is None or start >= end:
        raise RuntimeFailure("invalid_telemetry_window", "Telemetry polling bounds are invalid.")
    query = correlation_query(
        agent=agent,
        version=version,
        expectations=expectations,
    )
    deadline = monotonic() + timeout_seconds
    while True:
        rows = query_client.query(
            resource_id,
            query,
            start.astimezone(UTC),
            end.astimezone(UTC),
        )
        correlated = correlate_complete_traces(
            rows,
            expectations,
            agent=agent,
            version=version,
            start=start,
            end=end,
        )
        if correlated is not None and len(correlated) == len(expectations):
            return correlated
        if monotonic() >= deadline:
            raise RuntimeFailure(
                "telemetry_ingestion_timeout",
                "Application Insights did not contain every complete expected trace.",
                {"expected": len(expectations), "observed_operations": len(rows)},
                transient=True,
            )
        sleep(poll_seconds)
