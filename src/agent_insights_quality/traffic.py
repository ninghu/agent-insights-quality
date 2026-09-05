"""Read only the canonical v2 executable cases; session identity belongs to runtime."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .contracts import Attempt, Step, Target


@lru_cache(maxsize=8)
def traffic_validator(schema_root: Path, is_prompt: bool) -> Draft202012Validator:
    """Resolve local schemas only; no network retrieval or legacy schema fallback."""
    common = json.loads((schema_root / "traffic.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(common)
    registry = Registry().with_resource("traffic.schema.json", Resource.from_contents(common))
    schema = common
    if is_prompt:
        schema = json.loads(
            (schema_root / "prompt-traffic.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=registry)


def validate_traffic(
    document: dict[str, Any], *, is_prompt: bool, schema_root: Path,
) -> None:
    traffic_validator(schema_root, is_prompt).validate(document)
    requests = {item["id"]: item for item in document["requests"]}
    if len(requests) != len(document["requests"]):
        raise ValueError("Duplicate traffic request ID")
    if [item["index"] for item in document["attempts"]] != list(range(1, 11)):
        raise ValueError("Traffic attempts must be ordered from 1 through 10")
    referenced = set()
    for attempt in document["attempts"]:
        ids = attempt["setup_steps"] + attempt["probe_steps"]
        if not set(ids) <= requests.keys():
            raise ValueError("Unknown traffic request reference")
        referenced.update(ids)
    if referenced != requests.keys():
        raise ValueError("Traffic contains unused request definitions")


def load_attempts(target: Target) -> tuple[Attempt, ...]:
    document = json.loads(
        (target.version_root / "traffic.json").read_text(encoding="utf-8")
    )
    validate_traffic(
        document, is_prompt=target.is_prompt,
        schema_root=target.baseline_root.parents[2] / "schemas",
    )
    if (
        document["agent_name"] != target.unit_id.agent
        or document["logical_version"] != target.unit_id.logical_version
    ):
        raise ValueError("Traffic identity disagrees with target")
    requests = {item["id"]: item for item in document["requests"]}
    return tuple(
        Attempt(
            attempt["index"],
            tuple(
                Step(
                    f"{phase}-{position:02d}-{step_id}", phase,
                    copy.deepcopy(requests[step_id]["request"]["body"]),
                    copy.deepcopy(requests[step_id]["expected"]),
                )
                for phase in ("setup", "probe")
                for position, step_id in enumerate(attempt[f"{phase}_steps"], start=1)
            ),
            copy.deepcopy(attempt.get("parameters", {})),
        )
        for attempt in document["attempts"]
    )
