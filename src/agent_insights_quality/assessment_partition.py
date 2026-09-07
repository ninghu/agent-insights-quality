"""Lossless payload sizing and independent whole-conversation partitioning."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any


def payload_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def intern_payload(payload: dict) -> dict:
    """Replace repeated JSON subtrees with collision-free path references.

    References live in a separate table, never inside raw data. Expanding the
    table in order reconstructs the original JSON exactly, including page copies,
    both snapshot identities and their independently numbered evidence refs.
    """
    seen: dict[str, list[str | int]] = {}
    references = []
    document = deepcopy(payload)

    def visit(value: Any, path: list[str | int]) -> Any:
        if not isinstance(value, (dict, list)):
            return value
        serialized = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
        if len(serialized) >= 256:
            previous = seen.get(serialized)
            if previous is not None:
                reference = {"path": path, "source": previous}
                if len(json.dumps(reference)) + 4 < len(serialized):
                    references.append(reference)
                    return None
            else:
                seen[serialized] = path
        if isinstance(value, list):
            return [visit(child, [*path, index]) for index, child in enumerate(value)]
        return {key: visit(child, [*path, key]) for key, child in value.items()}

    document = visit(document, [])
    if not references:
        return deepcopy(payload)
    encoded = {
        "lossless_encoding": "json-path-references-v1",
        "document": document,
        "references": references,
    }
    return encoded if payload_size(encoded) < payload_size(payload) else deepcopy(payload)


def expand_payload(payload: dict) -> dict:
    """Expand the encoder's transport form for offline round-trip checks."""
    if payload.get("lossless_encoding") != "json-path-references-v1":
        return deepcopy(payload)
    document = deepcopy(payload["document"])
    for reference in payload["references"]:
        source = document
        for component in reference["source"]:
            source = source[component]
        destination = document
        for component in reference["path"][:-1]:
            destination = destination[component]
        destination[reference["path"][-1]] = deepcopy(source)
    return document


@dataclass(frozen=True)
class Partition:
    indices: tuple[int, ...]
    payload: dict
    oversized: bool


def _snapshots(payload: dict) -> dict[str, dict]:
    return {
        name: payload[name] for name in ("snapshot", "visible_snapshot") if name in payload
    }


def conversation_groups(payload: dict) -> tuple[tuple[int, ...], ...]:
    """Join attempts transitively through sessions, operations and shared raw refs."""
    parent: dict[tuple, tuple] = {}

    def find(key: tuple) -> tuple:
        parent.setdefault(key, key)
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != key:
            following = parent[key]
            parent[key] = root
            key = following
        return root

    def join(left: tuple, right: tuple) -> None:
        parent[find(right)] = find(left)

    for name, snapshot in _snapshots(payload).items():
        for row in snapshot["records"]:
            # Equal raw envelopes across snapshot-local row numbers are one dependency.
            raw = json.dumps(row["raw"], sort_keys=True, ensure_ascii=True, allow_nan=False)
            join(("ref", name, row["ref"]), ("raw", raw))
        for scope in snapshot["scopes"]:
            response = ("response", scope["response_id"])
            for operation in scope["operation_ids"]:
                if operation:
                    join(response, ("operation", operation))
            for ref in scope["anchor_refs"] + scope["evidence_refs"]:
                join(response, ("ref", name, ref))
    for attempt in payload["attempts"]:
        key = ("attempt", attempt["index"])
        find(key)
        for step in attempt["steps"]:
            execution = step["execution"] or {}
            response = execution.get("response") or {}
            session = execution.get("session_id")
            if session:
                join(key, ("session", session))
            if execution.get("response_id"):
                join(key, ("response", execution["response_id"]))
            for body in (step["request"], response):
                if not isinstance(body, dict):
                    continue
                previous = body.get("previous_response_id")
                if isinstance(previous, str) and previous:
                    join(key, ("response", previous))
                conversation = body.get("conversation")
                if isinstance(conversation, dict):
                    conversation = conversation.get("id")
                if isinstance(conversation, str) and conversation:
                    join(key, ("session", conversation))
    groups: dict[tuple, list[int]] = {}
    for attempt in payload["attempts"]:
        index = attempt["index"]
        groups.setdefault(find(("attempt", index)), []).append(index)
    return tuple(tuple(indices) for indices in groups.values())


def _subset(payload: dict, indices: tuple[int, ...]) -> dict:
    subset = {
        key: deepcopy(value) for key, value in payload.items()
        if key not in {"attempts", "snapshot", "visible_snapshot"}
    }
    subset["attempts"] = [
        deepcopy(attempt) for attempt in payload["attempts"] if attempt["index"] in indices
    ]
    responses = {
        step["execution"]["response_id"]
        for attempt in subset["attempts"] for step in attempt["steps"]
        if step["execution"] and step["execution"]["response_id"]
    }
    all_responses = {
        step["execution"]["response_id"]
        for attempt in payload["attempts"] for step in attempt["steps"]
        if step["execution"] and step["execution"]["response_id"]
    }
    for name, snapshot in _snapshots(payload).items():
        owned = {
            ref for scope in snapshot["scopes"]
            if scope["response_id"] in all_responses
            for ref in scope["anchor_refs"] + scope["evidence_refs"]
        }
        scopes = [
            scope for scope in snapshot["scopes"]
            if scope["response_id"] in responses or scope["response_id"] not in all_responses
        ]
        refs = {ref for scope in scopes for ref in scope["anchor_refs"] + scope["evidence_refs"]}
        subset[name] = {
            **{key: deepcopy(value) for key, value in snapshot.items()
               if key not in {"scopes", "records"}},
            "scopes": deepcopy(scopes),
            "records": [
                deepcopy(row) for row in snapshot["records"]
                if row["ref"] in refs or row["ref"] not in owned
            ],
        }
    subset["assessment_partition"] = {
        "attempt_indices": list(indices),
        "all_attempt_indices": [attempt["index"] for attempt in payload["attempts"]],
        "scope": "complete_independent_conversations",
        "unassigned_evidence": "retained_in_every_partition",
        "global_card_verdicts": "not_permitted",
    }
    return subset


def partition_payload(
    payload: dict, limit: int, *, intern: bool = False,
) -> tuple[Partition, ...]:
    """Pack complete components, preserving all raw records/scopes somewhere.

    Unassigned raw rows and scopes are shared context in every partition rather
    than assumed irrelevant. A component plus shared context that cannot fit is
    returned intact and explicitly oversized; nothing is trimmed to force a fit.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("Payload limit must be a positive integer")

    def prepare(indices: tuple[int, ...]) -> dict:
        subset = _subset(payload, indices)
        return intern_payload(subset) if intern else subset

    groups = conversation_groups(payload)
    partitions, pending = [], ()
    for group in groups:
        candidate = (*pending, *group)
        packed = prepare(candidate)
        if payload_size(packed) <= limit:
            pending = candidate
            continue
        if pending:
            partitions.append(Partition(pending, prepare(pending), False))
            pending = ()
        packed = prepare(group)
        if payload_size(packed) > limit:
            partitions.append(Partition(group, packed, True))
        else:
            pending = group
    if pending:
        partitions.append(Partition(pending, prepare(pending), False))
    return tuple(partitions)
