"""Independent caller context, not a conversation ID or an exported client span."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import re

from .errors import QualityError
from .state import StateError

ALGORITHM = "aiq-w3c-sha256-v1"
POLICY_KEY = "invocation-trace-context"
_ENABLED: ContextVar[bool] = ContextVar("aiq_invocation_trace_context", default=True)
_DOMAIN = b"agent-insights-quality/invocation-context/v1\0"


def traceparent(request_id: str) -> str:
    if not isinstance(request_id, str) or not request_id:
        raise QualityError("trace_context_request_id_invalid", request_accepted=False)
    def identity(purpose: bytes, length: int) -> str:
        value = hashlib.sha256(_DOMAIN + purpose + b"\0" + request_id.encode("utf-8")).hexdigest()[:length]
        return value if int(value, 16) else "0" * (length - 1) + "1"
    return f"00-{identity(b'trace', 32)}-{identity(b'parent', 16)}-01"


def validate_traceparent(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", value) is None
        or int(value[3:35], 16) == 0 or int(value[36:52], 16) == 0
    ):
        raise QualityError("trace_context_header_invalid", request_accepted=False)


def enabled_policy(value: dict | None) -> bool:
    if value is None:
        return False
    if set(value) != {"algorithm"} or value["algorithm"] not in (None, ALGORITHM):
        raise StateError("trace_context_policy_invalid")
    return value["algorithm"] == ALGORITHM


@contextmanager
def invocation_context(enabled: bool):
    token = _ENABLED.set(enabled)
    try:
        yield
    finally:
        _ENABLED.reset(token)


def invocation_headers(request_id: str) -> dict[str, str]:
    return {"traceparent": traceparent(request_id)} if _ENABLED.get() else {}


def planned_context(request_id: str, source_revision: str) -> dict:
    if not isinstance(source_revision, str) or not source_revision:
        raise QualityError("trace_context_source_invalid", request_accepted=False)
    return {
        "algorithm": ALGORITHM, "request_id": request_id,
        "traceparent": traceparent(request_id), "source_revision": source_revision,
        "provenance": "planned_before_invoke_not_wire_delivery_proof",
    }
