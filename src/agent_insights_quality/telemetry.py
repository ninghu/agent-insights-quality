from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from agent_insights_quality.contracts import Deployment, Invocation, QueryResult
from agent_insights_quality.errors import QualityError

_TABLES = "requests, dependencies, traces, exceptions, genAIContent"
_RESPONSE_FIELDS = (
    "gen_ai.response.id",
    "azure.ai.agentserver.response_id",
    "response_id",
)
_HOST_RESPONSE_FIELDS = ("azure.ai.agentserver.response_id", "response_id")


class QueryPort(Protocol):
    async def query(self, query: str, *, start: str, end: str) -> QueryResult: ...


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as error:
        raise QualityError("invalid_evidence_window") from error
    if result.tzinfo is None:
        raise QualityError("invalid_evidence_window")
    return result.astimezone(UTC)


def _literal(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualityError("invalid_telemetry_reference")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise QualityError("telemetry_record_type_invalid")


def discovery_query(response_ids: Iterable[str], start: str, end: str) -> str:
    references = ", ".join(_literal(value) for value in response_ids)
    if not references:
        raise QualityError("telemetry_responses_missing")
    predicates = " or ".join(
        f'tostring(customDimensions["{name}"]) in ({references})'
        for name in _RESPONSE_FIELDS
    )
    return (
        f"union isfuzzy=true withsource=telemetry_table {_TABLES}\n"
        f"| where timestamp >= datetime({_timestamp(start).isoformat()}) "
        f"and timestamp < datetime({_timestamp(end).isoformat()})\n"
        f"| where {predicates}"
    )


def operation_query(operation_ids: Iterable[str], start: str, end: str) -> str:
    references = ", ".join(_literal(value) for value in operation_ids)
    if not references:
        raise QualityError("telemetry_operations_missing")
    return (
        f"union isfuzzy=true withsource=telemetry_table {_TABLES}\n"
        f"| where timestamp >= datetime({_timestamp(start).isoformat()}) "
        f"and timestamp < datetime({_timestamp(end).isoformat()})\n"
        f"| where operation_Id in ({references})"
    )


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _properties(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("customDimensions", row.get("Properties", {}))
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise QualityError("telemetry_properties_invalid") from error
        if isinstance(parsed, dict):
            return parsed
    raise QualityError("telemetry_properties_invalid")


def _flat(record: Mapping[str, Any]) -> dict[str, Any]:
    if "columns" not in record and "values" not in record:
        return dict(record)
    columns, values = record.get("columns"), record.get("values")
    if (
        not isinstance(columns, (list, tuple))
        or not isinstance(values, (list, tuple))
        or len(columns) != len(values)
    ):
        raise QualityError("telemetry_columns_invalid")
    result: dict[str, Any] = {}
    for column, value in zip(columns, values, strict=True):
        name = column.get("name") if isinstance(column, Mapping) else None
        if not isinstance(name, str) or name in result:
            raise QualityError("telemetry_columns_invalid")
        result[name] = value
    return result


def _table(row: Mapping[str, Any]) -> str:
    value = _string(row.get("telemetry_table", row.get("Type", ""))).casefold()
    return {
        "apprequests": "requests",
        "appdependencies": "dependencies",
        "apptraces": "traces",
        "appexceptions": "exceptions",
        "appgenaicontent": "genaicontent",
    }.get(value, value)


def _operation(row: Mapping[str, Any]) -> str:
    return _string(row.get("operation_Id", row.get("OperationId", row.get("TraceId", ""))))


def _values(properties: Mapping[str, Any], names: Iterable[str]) -> set[str]:
    return {value for name in names if (value := _string(properties.get(name)))}


def _host_context(row: Mapping[str, Any], properties: Mapping[str, Any]) -> dict[str, set[str]]:
    return {
        **{
            name: _values(properties, (name,))
            for name in (
                "azure.ai.agentserver.session_id",
                "azure.ai.agentserver.conversation_id",
                "gen_ai.agent.id",
            )
        },
        "cloud_RoleInstance": _values(row, ("cloud_RoleInstance",)),
    }


@dataclass
class _Node:
    operation: str
    span: str
    parents: set[str] = field(default_factory=set)
    agents: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    responses: set[str] = field(default_factory=set)
    host_responses: set[str] = field(default_factory=set)
    operations: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    host_context: dict[str, set[str]] = field(default_factory=dict)

    @property
    def conflicting(self) -> bool:
        return any(len(values) > 1 for values in (
            self.parents, self.agents, self.versions, self.operations,
        ))


@dataclass(frozen=True)
class _Record:
    ref: str
    row: dict[str, Any]
    operation: str
    associated: str
    responses: set[str]
    host_responses: set[str]
    agents: set[str]
    versions: set[str]
    host_context: dict[str, set[str]]


@dataclass(frozen=True)
class ResponseScope:
    response_id: str
    operation_ids: tuple[str, ...]
    anchor_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    @property
    def attributable(self) -> bool:
        return bool(self.anchor_refs) and not self.reasons


@dataclass(frozen=True)
class Snapshot:
    observed_at: str
    window_start: str
    window_end: str
    records: tuple[dict[str, Any], ...]
    scopes: tuple[ResponseScope, ...]
    query_complete: bool
    gaps: tuple[str, ...] = ()

    @property
    def attributable_responses(self) -> frozenset[str]:
        return frozenset(
            scope.response_id for scope in self.scopes if scope.attributable
        )

    def to_private_dict(self) -> dict[str, Any]:
        return {"schema_version": "1.0", **_json_value(asdict(self))}

    @classmethod
    def from_private_dict(cls, value: Mapping[str, Any]) -> Snapshot:
        required = {
            "schema_version", "observed_at", "window_start", "window_end",
            "records", "scopes", "query_complete", "gaps",
        }
        if (
            set(value) != required or value.get("schema_version") != "1.0"
            or type(value["query_complete"]) is not bool
            or not all(isinstance(value[key], list) for key in ("records", "scopes", "gaps"))
        ):
            raise QualityError("evidence_snapshot_format_invalid")
        for key in ("observed_at", "window_start", "window_end"):
            _timestamp(value[key])
        refs = [
            item.get("ref") for item in value["records"] if isinstance(item, Mapping)
        ]
        if (
            len(refs) != len(value["records"])
            or any(not isinstance(ref, str) or not ref for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise QualityError("evidence_snapshot_format_invalid")
        responses: set[str] = set()
        for item in value["scopes"]:
            if (
                not isinstance(item, Mapping)
                or set(item) != {
                    "response_id", "operation_ids", "anchor_refs", "evidence_refs", "reasons",
                }
                or not isinstance(item["response_id"], str)
                or not item["response_id"]
                or item["response_id"] in responses
                or any(
                    not isinstance(item[key], list)
                    or any(not isinstance(part, str) for part in item[key])
                    for key in ("operation_ids", "anchor_refs", "evidence_refs", "reasons")
                )
                or not set(item["anchor_refs"] + item["evidence_refs"]) <= set(refs)
            ):
                raise QualityError("evidence_snapshot_format_invalid")
            responses.add(item["response_id"])
        return cls(
            observed_at=value["observed_at"],
            window_start=value["window_start"],
            window_end=value["window_end"],
            records=tuple(value["records"]),
            scopes=tuple(
                ResponseScope(
                    response_id=item["response_id"],
                    operation_ids=tuple(item["operation_ids"]),
                    anchor_refs=tuple(item["anchor_refs"]),
                    evidence_refs=tuple(item["evidence_refs"]),
                    reasons=tuple(item["reasons"]),
                )
                for item in value["scopes"]
            ),
            query_complete=value["query_complete"],
            gaps=tuple(value["gaps"]),
        )


def correlate(
    records: Iterable[Mapping[str, Any]],
    response_ids: Iterable[str],
    deployment: Deployment,
    *,
    observed_at: str,
    window_start: str,
    window_end: str,
    query_complete: bool = True,
    gaps: Iterable[str] = (),
) -> Snapshot:
    references = tuple(response_ids)
    if not references or len(set(references)) != len(references):
        raise QualityError("telemetry_responses_invalid")
    known = set(references)
    raw_records = tuple(
        {"ref": f"row-{index:06d}", "raw": _json_value(row)}
        for index, row in enumerate(records, 1)
    )
    nodes: dict[tuple[str, str], _Node] = {}
    normalized: list[_Record] = []
    host_claims: dict[tuple[str, str], set[str]] = defaultdict(set)
    problems = set(gaps)
    for item in raw_records:
        row = _flat(item["raw"])
        operation = _operation(row)
        try:
            properties = _properties(row)
        except QualityError:
            problems.add("telemetry_properties_invalid")
            properties = {}
        responses = _values(properties, _RESPONSE_FIELDS)
        explicit_host = _values(properties, _HOST_RESPONSE_FIELDS)
        agents = _values(properties, ("gen_ai.agent.name", "agent.name"))
        versions = _values(properties, ("gen_ai.agent.version", "agent.version"))
        host_context = _host_context(row, properties)
        table = _table(row)
        span = _string(row.get("id", row.get("Id", "")))
        parent = _string(row.get("operation_ParentId", row.get("ParentId", "")))
        associated = ""
        if table in {"requests", "dependencies"} and operation and span:
            associated = span
            node = nodes.setdefault((operation, span), _Node(operation, span))
            node.refs.add(item["ref"])
            node.parents.update({parent} if parent else set())
            node.agents.update(agents)
            node.versions.update(versions)
            node.responses.update(responses)
            node.host_responses.update(explicit_host)
            node.operations.update(_values(properties, ("gen_ai.operation.name",)))
            for name, values in host_context.items():
                node.host_context.setdefault(name, set()).update(values)
        elif table == "genaicontent":
            associated = _string(row.get("SpanId", row.get("span_id", span)))
        else:
            associated = parent or _string(properties.get("span_id"))
        host_claims[(operation, associated)].update(explicit_host)
        normalized.append(_Record(
            item["ref"], row, operation, associated, responses, explicit_host,
            agents, versions, host_context,
        ))

    children: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    declared_children: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for key, node in nodes.items():
        for parent in node.parents:
            declared_children[(node.operation, parent)].add(key)
            if (node.operation, parent) in nodes:
                children[(node.operation, parent)].add(key)

    def ancestors(key: tuple[str, str]) -> set[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        pending = [key]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            node = nodes.get(current)
            if node:
                pending.extend(
                    (node.operation, parent) for parent in node.parents
                    if (node.operation, parent) in nodes
                )
        return seen

    def descendants(
        key: tuple[str, str], response_id: str | None = None,
    ) -> set[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        pending = [key]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            node = nodes[current]
            if response_id and node.responses & known - {response_id}:
                continue
            seen.add(current)
            pending.extend(children[current])
        return seen

    def identity_matches(node: _Node) -> bool:
        return (
            not node.conflicting
            and (not node.agents or node.agents == {deployment.agent_name})
            and (not node.versions or node.versions == {deployment.provider_version})
        )

    def cyclic(key: tuple[str, str]) -> bool:
        seen: set[tuple[str, str]] = set()
        pending = [key]
        while pending:
            current = pending.pop()
            if current in seen:
                return True
            seen.add(current)
            node = nodes.get(current)
            if node:
                pending.extend((node.operation, parent) for parent in node.parents)
        return False

    scopes: list[ResponseScope] = []
    for reference in references:
        seeds = {
            (record.operation, record.associated)
            for record in normalized
            if reference in record.responses and (record.operation, record.associated) in nodes
            and (
                reference in record.host_responses
                or not nodes[(record.operation, record.associated)].operations & {"chat", "execute_tool"}
            )
        }
        related: set[tuple[str, str]] = set()
        for seed in seeds:
            related.update(ancestors(seed))
            related.update(descendants(seed))
        candidates = {
            key for key in related
            if nodes[key].operations == {"invoke_agent"}
            and identity_matches(nodes[key])
            and not nodes[key].responses & known - {reference}
        }
        # Nested hosting/framework anchors describe one call; unrelated exact roots do not.
        outer = {
            key for key in candidates
            if not (ancestors(key) - {key}) & candidates
        }
        reasons: set[str] = set()
        if not outer:
            reasons.add("invocation_anchor_missing")
        by_operation: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for key in outer:
            by_operation[key[0]].add(key)
        if any(len(values) > 1 for values in by_operation.values()):
            reasons.add("invocation_anchor_ambiguous")
        scope_nodes: set[tuple[str, str]] = set()
        context_nodes: set[tuple[str, str]] = set()
        for key in outer:
            scope_nodes.update(descendants(key, reference))
            context_nodes.update(ancestors(key))
        if any(nodes[key].conflicting for key in scope_nodes):
            reasons.add("span_identity_conflict")
        if any(cyclic(key) for key in scope_nodes):
            reasons.add("span_parent_cycle")
        invocation_instances = {
            instance
            for key in scope_nodes
            if nodes[key].operations == {"invoke_agent"} and identity_matches(nodes[key])
            for instance in nodes[key].host_context.get("cloud_RoleInstance", set())
        }

        def compatible_log(record: _Record) -> bool:
            if (
                record.host_responses and record.host_responses != {reference}
                or record.agents and record.agents != {deployment.agent_name}
                or record.versions and record.versions != {deployment.provider_version}
            ):
                return False
            instances = record.host_context["cloud_RoleInstance"]
            # The hosting root and its nested framework invocation may run on different hosts.
            if instances and invocation_instances and not instances <= invocation_instances:
                return False
            lineage = ancestors((record.operation, record.associated)) & scope_nodes
            for key in outer | lineage:
                if key[0] != record.operation:
                    continue
                owner = nodes[key]
                if (
                    not identity_matches(owner)
                    or owner.host_responses and owner.host_responses != {reference}
                    or any(len(values) > 1 for values in owner.host_context.values())
                ):
                    return False
                for name, values in record.host_context.items():
                    if name == "cloud_RoleInstance":
                        continue
                    expected = owner.host_context.get(name, set())
                    if len(values) > 1 or len(expected) > 1 or values and expected and values != expected:
                        return False
            return True

        def exact_host_orphan(record: _Record) -> bool:
            key = (record.operation, record.associated)
            if (
                reasons or len(outer) != 1 or not record.associated or key in nodes
                or record.host_responses != {reference}
                or record.agents != {deployment.agent_name}
                or record.versions != {deployment.provider_version}
                or host_claims[key] != {reference}
                or declared_children[key] - scope_nodes
            ):
                return False
            anchor = nodes[next(iter(outer))]
            if (
                record.operation != anchor.operation
                or anchor.agents != record.agents or anchor.versions != record.versions
            ):
                return False
            return True

        evidence = set()
        for record in normalized:
            if record.responses & known - {reference}:
                continue
            in_graph = (record.operation, record.associated) in scope_nodes | context_nodes
            if _table(record.row) == "traces":
                if not compatible_log(record):
                    continue
                if not in_graph and exact_host_orphan(record):
                    # Keep the raw log and its absent parent; it is not a synthetic span/edge.
                    evidence.add(record.ref)
                    problems.add("attributed_log_parent_missing")
            if in_graph:
                evidence.add(record.ref)
        scopes.append(ResponseScope(
            response_id=reference,
            operation_ids=tuple(sorted({key[0] for key in outer})),
            anchor_refs=tuple(sorted(
                record_ref for key in outer for record_ref in nodes[key].refs
            )),
            evidence_refs=tuple(sorted(evidence)),
            reasons=tuple(sorted(reasons)),
        ))
    return Snapshot(
        observed_at=observed_at,
        window_start=window_start,
        window_end=window_end,
        records=raw_records,
        scopes=tuple(scopes),
        query_complete=query_complete,
        gaps=tuple(sorted(problems)),
    )


async def collect_snapshot(
    port: QueryPort,
    deployment: Deployment,
    invocations: Iterable[Invocation],
    *,
    observed_at: datetime | None = None,
    margin_seconds: int = 30,
    operation_batch_size: int = 100,
) -> Snapshot:
    receipts = tuple(invocations)
    if not receipts or margin_seconds < 0 or operation_batch_size < 1:
        raise QualityError("evidence_collection_arguments_invalid")
    response_ids = tuple(
        receipt.response_id for receipt in receipts if receipt.response_id
    )
    if len(set(response_ids)) != len(response_ids):
        raise QualityError("duplicate_endpoint_response")
    if any(_timestamp(item.completed_at) < _timestamp(item.started_at) for item in receipts):
        raise QualityError("invalid_evidence_window")
    start = (min(_timestamp(item.started_at) for item in receipts)
             - timedelta(seconds=margin_seconds)).isoformat()
    end = (max(_timestamp(item.completed_at) for item in receipts)
           + timedelta(seconds=margin_seconds)).isoformat()
    moment = observed_at or datetime.now(UTC)
    if moment.tzinfo is None:
        raise QualityError("invalid_evidence_window")
    if not response_ids:
        return Snapshot(
            moment.isoformat(), start, end, (), (), True, ("endpoint_responses_missing",),
        )
    discovery = await port.query(
        discovery_query(response_ids, start, end), start=start, end=end,
    )
    operations = sorted({
        operation
        for row in discovery.records
        if (operation := _operation(_flat(row)))
    })
    records: list[dict[str, Any]] = []
    gaps: set[str] = set()
    complete = discovery.complete
    if not discovery.complete:
        gaps.add(discovery.error_code or "discovery_incomplete")

    async def fetch(batch: list[str]) -> None:
        nonlocal complete
        result = await port.query(operation_query(batch, start, end), start=start, end=end)
        if not result.complete and result.error_code == "result_too_large" and len(batch) > 1:
            midpoint = len(batch) // 2
            await fetch(batch[:midpoint])
            await fetch(batch[midpoint:])
            return
        records.extend(result.records)
        if not result.complete:
            complete = False
            gaps.add(result.error_code or "raw_collection_incomplete")

    for index in range(0, len(operations), operation_batch_size):
        await fetch(operations[index:index + operation_batch_size])
    if not operations:
        gaps.add("operations_missing")
    moment = observed_at or datetime.now(UTC)
    return correlate(
        records, response_ids, deployment,
        observed_at=moment.astimezone(UTC).isoformat(),
        window_start=start, window_end=end,
        query_complete=complete,
        gaps=gaps,
    )
