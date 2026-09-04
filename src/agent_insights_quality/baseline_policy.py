from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BaselineTerminalDecision:
    status: str
    unknown_count: int


def baseline_terminal_decision(
    *,
    request_count: int,
    terminal_mode: str,
    trace_evidence: Mapping[str, Any],
    strict_evidence: bool,
) -> BaselineTerminalDecision:
    if request_count <= 0:
        return BaselineTerminalDecision("incomplete", request_count)
    if int(trace_evidence.get("unhandled_error_count") or 0) != 0:
        return BaselineTerminalDecision("failed", 0)
    fields = [
        "terminal_response_count",
        "terminal_success_count",
        "terminal_output_count",
    ]
    if terminal_mode == "explicit_span_attributes":
        fields.extend(
            [
                "explicit_terminal_success_count",
                "explicit_terminal_output_count",
            ]
        )
    else:
        fields.append("assistant_response_count")
    counts = [int(trace_evidence.get(field) or 0) for field in fields]
    if any(count < 0 or count > request_count for count in counts):
        return BaselineTerminalDecision("failed", 0)
    unknown_count = max(request_count - count for count in counts)
    if unknown_count == 0:
        return BaselineTerminalDecision("complete", 0)
    if strict_evidence and unknown_count == 1:
        return BaselineTerminalDecision("accepted_unknown", 1)
    return BaselineTerminalDecision("incomplete", unknown_count)
