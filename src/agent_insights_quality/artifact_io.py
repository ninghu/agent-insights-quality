from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_insights_quality.contracts import ContractError


SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def content_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def verified_hash(value: Mapping[str, Any], field: str, label: str) -> None:
    actual = value.get(field)
    if not isinstance(actual, str) or not SHA256_PATTERN.fullmatch(actual):
        raise ContractError(f"{label}: {field} must be a sha256 reference")
    unsigned = dict(value)
    del unsigned[field]
    if actual != content_hash(unsigned):
        raise ContractError(f"{label}: {field} does not match canonical content")


def read_json_object(path: Path, label: str | None = None) -> dict[str, Any]:
    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number: {value}")

    try:
        text = path.read_text(encoding="ascii")
        value = json.loads(
            text,
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ContractError(f"{label or path}: invalid strict JSON: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label or path}: expected a JSON object")
    return value


def write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    write_bytes_atomic(
        path,
        json.dumps(value, indent=2, sort_keys=True).encode("ascii") + b"\n",
    )


def bounded_text(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field}: expected non-empty text")
    text = "".join(
        character
        for character in value
        if character in "\n\r\t" or ord(character) >= 32
    )
    return text[:limit]
