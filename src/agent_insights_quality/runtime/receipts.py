from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import RuntimeFailure

_FORBIDDEN_KEY = re.compile(
    r"(?i)(?:access|refresh)?token|authorization|connection.?string|(?:api|account|client)?key|"
    r"client.?secret|password|credential|subscription.?id|tenant.?id|resource.?id|endpoint|url"
)
_FORBIDDEN_VALUE = re.compile(
    r"(?i)(?:bearer\s+|instrumentationkey=|accountkey=|sharedaccesssignature=|"
    r"https?://|/subscriptions/)"
)


def ensure_public_safe(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _FORBIDDEN_KEY.search(key):
                raise RuntimeFailure(
                    "private_value_in_receipt",
                    f"Receipt field {path}.{key} is not public-safe.",
                )
            ensure_public_safe(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            ensure_public_safe(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and _FORBIDDEN_VALUE.search(value):
        raise RuntimeFailure(
            "private_value_in_receipt",
            f"Receipt value at {path} is not public-safe.",
        )


def write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_public_safe(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeFailure("invalid_receipt", "Runtime receipt is missing or invalid.") from error
    if not isinstance(payload, dict):
        raise RuntimeFailure("invalid_receipt", "Runtime receipt must be a JSON object.")
    ensure_public_safe(payload)
    return payload
