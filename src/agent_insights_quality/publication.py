"""Optional, private publication outbox; never orchestrates qualification.

Hold RuntimeStore.ownership() while enqueueing/flushing. Use a stable code-owned
framework run alias, not a provider ID, and the actual reviewed plan.
Each instance flushes only its run, including events queued before a final result
exists. Recreate it with the same plan/ID to resume. There are no repository,
GitHub, schema-management, or test-mode side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
from itertools import groupby
import json
import re
import threading
from typing import Any, Protocol
from urllib.parse import urlsplit

from .errors import QualityError
from .events import _directory_lock, project_event
from .privacy import public_projection, validate_public_projection
from .results import PlannedUnit, QualityResult
from .state import RecordStore, StateConflict


REPORT_TABLE = "QualityReportsV1"
EVENT_TABLE = "QualityOperationsV1"
_REPORT_COLUMNS = (
    ("ReportDate", "datetime"), ("FrameworkRunId", "string"), ("SourceCommit", "string"),
    ("Region", "string"), ("ContentHash", "string"), ("PayloadVersion", "string"), ("Payload", "dynamic"),
)
_EVENT_COLUMNS = (
    ("FrameworkRunId", "string"), ("Profile", "string"), ("EventId", "string"),
    ("ContentHash", "string"), ("EventVersion", "string"), ("Event", "dynamic"),
)
_HASH = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_STATES = frozenset({"pending", "unknown", "delivered", "conflict"})


class PublicationError(QualityError):
    """Public-safe adapter error; no provider response or exception text."""


class AdxUnavailable(PublicationError):
    """A transport failure does not establish whether ingestion happened."""

    def __init__(self, *, request_accepted: bool | None = None) -> None:
        super().__init__("adx_delivery_failed", request_accepted=request_accepted)


class AdxClient(Protocol):
    """Synchronous boundary. query must return a COMPLETE result or raise.

    No SDK types cross this boundary. manage returns only after the service call,
    but acceptance alone is not delivery proof. Transport failures raise
    AdxUnavailable (or OSError); arbitrary programming errors are not swallowed.
    """

    def query(self, statement: str) -> Sequence[Mapping[str, Any]]: ...
    def manage(self, statement: str) -> None: ...
    def close(self) -> None: ...


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _run_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 64 and _RUN_ID.fullmatch(value) is not None


def _metadata(report_date: str, source_commit: str, region: str) -> dict[str, str]:
    if (
        not isinstance(report_date, str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", report_date) is None
        or not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or region not in ("swedencentral", "SwedenCentral", "Sweden Central")
    ):
        raise PublicationError("publication_metadata_invalid")
    try:
        date.fromisoformat(report_date)
    except ValueError as error:
        raise PublicationError("publication_metadata_invalid") from error
    return {"report_date": report_date, "source_commit": source_commit, "region": region}


def validate_public_report(
    value: Mapping[str, Any], *, allowed_units: Iterable[PlannedUnit],
) -> dict[str, Any]:
    """Shared JSON/ADX/Git boundary; the allowed plan is reviewed caller input.

    Region preserves the actual ARM-resolved display/key for the current Sweden
    environment, not a fallback. Additional regions require explicit review.
    This validates, but deliberately does not introduce a second score engine.
    """
    if (
        not isinstance(value, Mapping)
        or set(value) != {
            "schema_version", "report_date", "framework_run_id", "source_commit", "region", "report",
        }
        or value["schema_version"] != "1" or not _run_id(value["framework_run_id"])
    ):
        raise PublicationError("publication_report_invalid")
    metadata = _metadata(value["report_date"], value["source_commit"], value["region"])
    payload = validate_public_projection(value["report"], allowed_units=allowed_units)
    if not payload["team_report_eligible"] or payload["status"] not in ("Full", "Partial"):
        raise PublicationError("publication_report_ineligible")
    return {
        "schema_version": "1", "framework_run_id": value["framework_run_id"],
        **metadata, "report": payload,
    }


def build_public_report(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit], report_date: str,
    framework_run_id: str, source_commit: str, region: str,
) -> dict[str, Any]:
    """The ONE public report envelope, with no outbox/provider implementation data."""
    plan = tuple(allowed_units)
    return validate_public_report({
        "schema_version": "1", "framework_run_id": framework_run_id,
        **_metadata(report_date, source_commit, region),
        "report": public_projection(result, allowed_units=plan),
    }, allowed_units=plan)


@dataclass(frozen=True)
class FlushResult:
    attempted: int = 0
    delivered: int = 0
    pending: int = 0
    unknown: int = 0
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    skipped: bool = False


class PublicationOutbox:
    def __init__(
        self,
        records: RecordStore,
        *,
        framework_run_id: str,
        profile: str,
        allowed_units: Iterable[PlannedUnit],
        test_run: bool = False,
    ) -> None:
        plan = tuple(allowed_units)
        if (
            not isinstance(records, RecordStore)
            or not _run_id(framework_run_id)
            or profile not in ("daily", "staging") or type(test_run) is not bool
            or not plan or any(not isinstance(unit, PlannedUnit) for unit in plan)
            or len({unit.unit_id for unit in plan}) != len(plan)
        ):
            raise PublicationError("publication_context_invalid")
        if (
            records._runtime.environment != profile
            or records.directory.parent != records._runtime.directory / "outboxes"
        ):
            raise PublicationError("publication_store_not_outbox")
        self.records, self.framework_run_id = records, framework_run_id
        self.profile, self.allowed_units, self.test_run = profile, plan, test_run
        # Share the delivery lock across instances, without blocking local enqueue
        # or logging during a remote query. RecordStore serializes atomic writes.
        self._flush_lock = _directory_lock(records.directory / "publication-delivery")

    def _key(self, collection: str, identity: str) -> str:
        return f"{collection}/{self.framework_run_id}/{identity}"

    def _context(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "allowed_units": sorted(
                ({"unit_id": unit.unit_id.to_dict(), "expected_issue_alias": unit.expected_issue_alias}
                 for unit in self.allowed_units),
                key=lambda unit: (unit["unit_id"]["agent"], unit["unit_id"]["logical_version"]),
            ),
        }

    def _validate_context(self, *, required: bool) -> None:
        context = self.records.read_completed(f"context/{self.framework_run_id}", missing_ok=not required)
        if context is not None and context != self._context():
            raise StateConflict()

    def _save_request(self, identity: str, body: dict[str, Any]) -> str:
        request = {"body": body, "content_hash": _digest(body)}
        if len(_json(request)) > 1_000_000:
            raise PublicationError("publication_request_too_large")
        self.records.save_completed(f"context/{self.framework_run_id}", self._context())
        self.records.save_completed(self._key("requests", identity), request)
        return identity

    def queue_report(
        self, result: QualityResult, *, report_date: str, source_commit: str, region: str,
    ) -> str | None:
        """Persist one immutable public DTO. Failed/staging reports are forbidden.

        Explicit test mode returns None without reading/writing the store. The
        result comes from aggregate_results, never a raw/model-supplied mapping.
        """
        if self.test_run:
            return None
        if self.profile != "daily":
            raise PublicationError("publication_report_not_daily")
        body = build_public_report(
            result, allowed_units=self.allowed_units, framework_run_id=self.framework_run_id,
            report_date=report_date, source_commit=source_commit, region=region,
        )
        return self._save_request("report", body)

    def queue_event(self, event: Mapping[str, Any]) -> str | None:
        """RunLogger callback: validate once more, then durably deduplicate replay."""
        if self.test_run:
            return None
        body = {
            "kind": "event", "framework_run_id": self.framework_run_id,
            "profile": self.profile, "payload_version": "1",
            "event": project_event(event, allowed_units=(unit.unit_id for unit in self.allowed_units)),
        }
        # Only public fields participate; no provider identity is introduced.
        return self._save_request("evt-" + _digest(body), body)

    def _request(self, identity: str) -> dict[str, Any]:
        request = self.records.read_completed(self._key("requests", identity))
        if not isinstance(request, dict) or set(request) != {"body", "content_hash"}:
            raise PublicationError("publication_request_invalid")
        body = request["body"]
        if not isinstance(body, dict) or (
            body.get("framework_run_id") != self.framework_run_id
            or request["content_hash"] != _digest(body)
            or len(_json(request)) > 1_000_000
        ):
            raise PublicationError("publication_request_invalid")
        if identity == "report":
            validate_public_report(body, allowed_units=self.allowed_units)
            if self.profile != "daily":
                raise PublicationError("publication_request_invalid")
        else:
            if (
                set(body) != {"kind", "framework_run_id", "profile", "payload_version", "event"}
                or body["kind"] != "event" or body["profile"] != self.profile
                or body["payload_version"] != "1"
                or identity != "evt-" + _digest(body)
            ):
                raise PublicationError("publication_request_invalid")
            project_event(body["event"], allowed_units=(unit.unit_id for unit in self.allowed_units))
        return request

    def read_request(self, identity: str) -> dict[str, Any] | None:
        """Read the validated ADX DTO; never an app/GitHub upload request."""
        if self.test_run:
            return None
        self._validate_context(required=True)
        return self._request(identity)

    def _identities(self) -> list[str]:
        directory = self.records.directory / "completed" / "requests" / self.framework_run_id
        # Completed request files, not a mutable index, are the durable queue.
        return sorted(path.stem for path in directory.glob("*.json"))

    def _state(self, identity: str, request: dict[str, Any]) -> str:
        value = self.records.read(self._key("outcomes", identity), missing_ok=True)
        if value is None:
            return "pending"
        if (
            set(value) != {"state", "content_hash"}
            or not isinstance(value["state"], str) or value["state"] not in _STATES
            or value["content_hash"] != request["content_hash"]
        ):
            raise PublicationError("publication_outcome_invalid")
        return value["state"]

    def _save_state(self, identity: str, request: dict[str, Any], state: str) -> None:
        save = self.records.save_completed if state in ("delivered", "conflict") else self.records.save_progress
        save(self._key("outcomes", identity), {"state": state, "content_hash": request["content_hash"]})

    def _reconcile(
        self, client: AdxClient, requests: dict[str, dict[str, Any]], *, reports: bool,
    ) -> None:
        table, column = (REPORT_TABLE, "FrameworkRunId") if reports else (EVENT_TABLE, "EventId")
        ids = [self.framework_run_id] if reports else list(requests)
        statement = (
            f"{table}\n| where FrameworkRunId == {_json(self.framework_run_id)}"
            f"\n| where {column} in ({','.join(_json(identity) for identity in ids)})"
            f"\n| project {column}, ContentHash"
        )
        rows = client.query(statement)
        seen: dict[str, set[str]] = {}
        for row in rows:
            if (
                not isinstance(row, Mapping) or set(row) != {column, "ContentHash"}
                or row[column] not in ids or not isinstance(row["ContentHash"], str)
                or _HASH.fullmatch(row["ContentHash"]) is None
            ):
                raise AdxUnavailable()
            identity = "report" if reports else row[column]
            seen.setdefault(identity, set()).add(row["ContentHash"])
        for identity, hashes in seen.items():
            state = "delivered" if hashes == {requests[identity]["content_hash"]} else "conflict"
            self._save_state(identity, requests[identity], state)

    def _row(self, identity: str, request: dict[str, Any]) -> dict[str, Any]:
        body = request["body"]
        common = {"FrameworkRunId": self.framework_run_id, "ContentHash": request["content_hash"]}
        if identity == "report":
            return {
                **common, "ReportDate": body["report_date"], "SourceCommit": body["source_commit"],
                "Region": body["region"], "PayloadVersion": "1", "Payload": body["report"],
            }
        return {
            **common, "EventId": identity, "Profile": self.profile,
            "EventVersion": "1", "Event": body["event"],
        }

    def flush(
        self, client: AdxClient, *, batch_size: int = 100, max_batches: int = 4,
    ) -> FlushResult:
        """Bounded best effort; never retry an unknown ingest merely on absence.

        Pre-submit query failure leaves pending requests retryable. Every member
        of a submitted batch is checkpointed unknown BEFORE manage. Afterwards,
        query-visible matching IDs complete individually, even after a partial
        failure. Invisible/ambiguous items remain unknown for later read-only
        reconciliation. Checkpoint failures propagate and halt further writes.
        Caller owns client.close(); this method never logs recursively.
        """
        if self.test_run:
            return FlushResult(skipped=True)
        if (
            type(batch_size) is not int or not 1 <= batch_size <= 100
            or type(max_batches) is not int or not 1 <= max_batches <= 100
        ):
            raise PublicationError("publication_batch_invalid")
        with self._flush_lock:
            # Validate ALL queued data before any network call, even on replay.
            identities = self._identities()
            self._validate_context(required=bool(identities))
            requests = {identity: self._request(identity) for identity in identities}
            candidates = [
                identity for identity, request in requests.items()
                if self._state(identity, request) not in ("delivered", "conflict")
            ]
            cursor = self.records.read(f"cursor/{self.framework_run_id}", missing_ok=True)
            if cursor is not None:
                if set(cursor) != {"after"} or not isinstance(cursor["after"], str):
                    raise PublicationError("publication_cursor_invalid")
                candidates = ([identity for identity in candidates if identity > cursor["after"]]
                              + [identity for identity in candidates if identity <= cursor["after"]])
            groups = [list(group) for _, group in groupby(candidates, key=lambda identity: identity == "report")]
            batches = [group[start:start + batch_size] for group in groups
                       for start in range(0, len(group), batch_size)][:max_batches]
            attempted, failed = 0, False
            for identities in batches:
                batch = {identity: requests[identity] for identity in identities}
                reports = identities[0] == "report"
                try:
                    self._reconcile(client, batch, reports=reports)
                except (AdxUnavailable, OSError):
                    failed = True
                    self.records.save_progress(f"cursor/{self.framework_run_id}", {"after": identities[-1]})
                    continue
                pending = {identity: request for identity, request in batch.items()
                           if self._state(identity, request) == "pending"}
                if not pending:
                    self.records.save_progress(f"cursor/{self.framework_run_id}", {"after": identities[-1]})
                    continue
                for identity, request in pending.items():
                    self._save_state(identity, request, "unknown")
                table = REPORT_TABLE if reports else EVENT_TABLE
                columns = _REPORT_COLUMNS if reports else _EVENT_COLUMNS
                command = f".append {table} <|\ndatatable(Row:dynamic) [\n"
                command += ",\n".join(f"dynamic({_json(self._row(identity, request))})"
                                       for identity, request in pending.items())
                command += "\n]\n| project " + ", ".join(
                    f"{name} = " + (f"Row.{name}" if kind == "dynamic" else f"to{kind}(Row.{name})")
                    for name, kind in columns
                )
                attempted += len(pending)
                try:
                    client.manage(command)
                except AdxUnavailable as error:
                    failed = True
                    if error.request_accepted is False:
                        for identity, request in pending.items():
                            self._save_state(identity, request, "pending")
                except OSError:
                    failed = True
                try:
                    self._reconcile(client, pending, reports=reports)
                except (AdxUnavailable, OSError):
                    failed = True
                self.records.save_progress(f"cursor/{self.framework_run_id}", {"after": identities[-1]})
            states = {identity: self._state(identity, request) for identity, request in requests.items()}
            conflicts = tuple(identity for identity, state in states.items() if state == "conflict")
            pending_count, unknown = list(states.values()).count("pending"), list(states.values()).count("unknown")
            return FlushResult(
                attempted=attempted, delivered=list(states.values()).count("delivered"),
                pending=pending_count, unknown=unknown, conflicts=conflicts,
                warnings=("adx_delivery_failed",) if failed or pending_count or unknown or conflicts else (),
            )

    async def flush_async(
        self, client: AdxClient, *, batch_size: int = 100, max_batches: int = 4,
    ) -> FlushResult:
        """Keep SDK I/O off the event loop; drain the bounded worker on cancel.

        Do not release runtime ownership or close the client while a canceled
        to_thread worker is still performing checkpointed side effects.
        """
        if self.test_run:
            return FlushResult(skipped=True)
        task = asyncio.create_task(asyncio.to_thread(
            self.flush, client, batch_size=batch_size, max_batches=max_batches,
        ))
        canceled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                canceled = True
        result = task.result()
        if canceled:
            raise asyncio.CancelledError()
        return result


class AzureCliAdxClient:
    """Lazy azure-kusto-data client using the existing Azure CLI login.

    Configuration remains private. Constructing/closing an unused instance does
    not load the optional SDK, discover resources, authenticate, or call Azure.
    Only query and append-to-existing-table are used; Bicep owns schema deployment.
    """

    def __init__(self, cluster_uri: str, database: str) -> None:
        uri = urlsplit(cluster_uri)
        if (
            uri.scheme != "https" or not uri.hostname or uri.username or uri.password
            or uri.query or uri.fragment or uri.path not in ("", "/")
            or not isinstance(database, str) or not database or len(database) > 128
            or not database.isprintable()
        ):
            raise PublicationError("publication_adx_config_invalid")
        self._cluster_uri, self._database = cluster_uri, database
        self._client: Any = None
        self._lock = threading.RLock()
        self._closed = False

    def _execute(self, statement: str, *, management: bool) -> Any:
        with self._lock:
            if self._closed:
                raise PublicationError("publication_client_closed")
            try:
                from azure.kusto.data import ClientRequestProperties, KustoClient, KustoConnectionStringBuilder
                from azure.kusto.data.exceptions import KustoClientError, KustoServiceError
            except ImportError as error:
                raise AdxUnavailable(request_accepted=False) from error
            try:
                if self._client is None:
                    connection = KustoConnectionStringBuilder.with_az_cli_authentication(self._cluster_uri)
                    self._client = KustoClient(connection)
                    self._client.set_http_retries(0)
                properties = ClientRequestProperties()
                properties.set_option("servertimeout", timedelta(seconds=30))
                properties.set_option("norequesttimeout", False)
                if not management:
                    properties.set_option("queryconsistency", "strongconsistency")
                    properties.set_option("notruncation", True)
                execute = self._client.execute_mgmt if management else self._client.execute_query
                response = execute(self._database, statement, properties)
                if response.get_exceptions():
                    raise AdxUnavailable()
                return response
            except (KustoClientError, KustoServiceError, OSError) as error:
                raise AdxUnavailable() from error

    def query(self, statement: str) -> list[dict[str, Any]]:
        response = self._execute(statement, management=False)
        if len(response.primary_results) != 1:
            raise AdxUnavailable()
        table = response.primary_results[0]
        return [{column.column_name: row[column.column_name] for column in table.columns}
                for row in table]

    def manage(self, statement: str) -> None:
        self._execute(statement, management=True)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._closed = True
