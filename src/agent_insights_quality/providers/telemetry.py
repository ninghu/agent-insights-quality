from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol

from agent_insights_quality.contracts import JsonObject, QueryResult
from agent_insights_quality.errors import QualityError


class LogsReader(Protocol):
    async def query(self, query: str, *, start: str, end: str) -> QueryResult: ...


def time_bounds(start: str, end: str) -> tuple[datetime, datetime]:
    try:
        first, last = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in (start, end)
        )
    except ValueError:
        raise QualityError("telemetry_time_invalid") from None
    if first.tzinfo is None or last.tzinfo is None or first >= last:
        raise QualityError("telemetry_time_invalid")
    return first, last


def table_records(tables: Any) -> tuple[JsonObject, ...]:
    records: list[JsonObject] = []
    for table in tables:
        columns = [
            {"name": column}
            if isinstance(column, str)
            else {
                "name": column.name,
                "type": getattr(column, "type", None),
            }
            for column in table.columns
        ]
        for row in table.rows:
            records.append(
                {"table": table.name, "columns": columns, "values": list(row)}
            )
    return tuple(records)


class AzureLogsReader:
    """Read-only raw tables; partial data is returned as incomplete, never as success."""

    def __init__(
        self, resource_id: str, *, client: Any = None, credential: Any = None
    ) -> None:
        self.resource_id = resource_id
        self._client = client
        self._credential = credential

    async def query(self, query: str, *, start: str, end: str) -> QueryResult:
        bounds = time_bounds(start, end)
        return await asyncio.to_thread(self._query, query, bounds)

    def _query(self, query: str, bounds: tuple[datetime, datetime]) -> QueryResult:
        try:
            from azure.core.exceptions import AzureError
            from azure.identity import DefaultAzureCredential
            from azure.monitor.query import LogsQueryClient
        except ImportError:
            raise QualityError("azure_logs_unavailable") from None
        if self._client is None:
            self._client = LogsQueryClient(
                self._credential
                if self._credential is not None
                else DefaultAzureCredential()
            )
        try:
            result = self._client.query_resource(
                self.resource_id, query, timespan=bounds, server_timeout=180
            )
        except AzureError:
            raise QualityError("telemetry_query_failed", retryable=True) from None
        return self.convert(result)

    @staticmethod
    def convert(result: Any) -> QueryResult:
        status = getattr(result.status, "value", result.status)
        if status == "Success":
            return QueryResult(table_records(result.tables), True)
        if status == "PartialError":
            return QueryResult(
                table_records(result.partial_data or []),
                False,
                "telemetry_query_partial",
            )
        return QueryResult((), False, "telemetry_query_status_unknown")
