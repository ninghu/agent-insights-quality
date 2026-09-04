from agent_insights_quality.baseline_policy import baseline_terminal_decision


def _trace(*, terminal_count: int, unhandled_errors: int = 0) -> dict:
    return {
        "terminal_response_count": terminal_count,
        "terminal_success_count": terminal_count,
        "terminal_output_count": terminal_count,
        "explicit_terminal_success_count": terminal_count,
        "explicit_terminal_output_count": terminal_count,
        "unhandled_error_count": unhandled_errors,
    }


def test_single_terminal_unknown_is_accepted_only_with_strict_evidence() -> None:
    accepted = baseline_terminal_decision(
        request_count=10,
        terminal_mode="explicit_span_attributes",
        trace_evidence=_trace(terminal_count=9),
        strict_evidence=True,
    )
    incomplete = baseline_terminal_decision(
        request_count=10,
        terminal_mode="explicit_span_attributes",
        trace_evidence=_trace(terminal_count=9),
        strict_evidence=False,
    )

    assert accepted.status == "accepted_unknown"
    assert accepted.unknown_count == 1
    assert incomplete.status == "incomplete"


def test_two_terminal_unknowns_remain_incomplete() -> None:
    decision = baseline_terminal_decision(
        request_count=10,
        terminal_mode="explicit_span_attributes",
        trace_evidence=_trace(terminal_count=8),
        strict_evidence=True,
    )

    assert decision.status == "incomplete"
    assert decision.unknown_count == 2


def test_unhandled_error_is_definitive_failure() -> None:
    decision = baseline_terminal_decision(
        request_count=10,
        terminal_mode="explicit_span_attributes",
        trace_evidence=_trace(terminal_count=10, unhandled_errors=1),
        strict_evidence=True,
    )

    assert decision.status == "failed"
