from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace

import pytest

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_judge import (
    JUDGE_MODEL,
    JUDGE_PROMPT_VERSION,
    ValidationJudge,
    build_judge_input,
    judge_prompt_digest,
    sanitize_collected_trace_evidence,
    stamp_judge_output,
    summarize_reviewed_scenario,
    validate_judge_input,
    validate_judge_output,
)

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40


def _authority(kind: str = "issue") -> SimpleNamespace:
    return SimpleNamespace(
        authority_id="issue-001" if kind == "issue" else "weather-agent/v0",
        authority_kind=kind,
        canonical_agent="weather-agent",
        logical_version="issue-001" if kind == "issue" else "v0",
    )


def _scenario(mode: str = "model_mediated") -> dict:
    n = 7 if mode == "model_mediated" else 5
    return {
        "id": "synthetic-review",
        "execution_digest": HASH,
        "validation_mode": mode,
        "n": n,
        "k": 5,
        "attempts": [
            {
                "index": index,
                "parameters": {"case": index},
                "setup_steps": [],
                "probe_steps": [
                    {
                        "id": f"probe-{index}",
                        "request": {
                            "method": "POST",
                            "path": "/responses",
                            "headers": {"content-type": "application/json"},
                            "body": {
                                "input": [
                                    {
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": f"Synthetic case {index}.",
                                            }
                                        ],
                                    }
                                ],
                                "conversation": {"id": "$validation_conversation"},
                            },
                        },
                        "expected": {
                            "http_status": 200,
                            "semantic_assertions": {
                                "required_terms_all": ["synthetic"]
                            },
                            "trace_assertions": [
                                {
                                    "name": "synthetic_trace",
                                    "kind": "operation_sequence",
                                    "operations": ["invoke_agent", "chat"],
                                }
                            ],
                            "identity_assertions": {
                                "agent_name": "$runtime_agent_name",
                                "agent_version": "$runtime_agent_version",
                            },
                        },
                    }
                ],
            }
            for index in range(1, n + 1)
        ],
    }


def _issue() -> dict:
    return {
        "id": "issue-001",
        "title": "Synthetic issue",
        "root_cause": "A synthetic behavior differs from its reviewed contract.",
        "category": "output_quality",
        "severity": "medium",
        "expected_fix": "Restore the reviewed synthetic behavior.",
    }


def _binding() -> dict:
    return {
        "repository": "ninghu/agent-insights-quality",
        "pr_number": 63,
        "cycle_id": "validation-cycle-0001",
        "commit_sha": HEAD,
        "validation_digest": HASH,
        "runtime_topology_digest": HASH,
    }


def _mechanical(role: str, index: int = 1) -> dict:
    name = (
        "weather-agent-baseline-cycle"
        if role in {"baseline", "paired_v0"}
        else "weather-agent-issue-001-cycle"
    )
    window_start = f"2026-08-31T10:0{index}:00+00:00"
    window_end = f"2026-08-31T10:0{index}:01+00:00"
    root_reference = content_hash({"root": role, "index": index})
    child_reference = content_hash({"child": role, "index": index})
    collected = {
        "operation_count": 1,
        "span_count": 2,
        "operations": [
            {
                "operation_reference": content_hash(
                    {"operation": role, "index": index}
                ),
                "spans": [
                    {
                        "sequence": 1,
                        "span_reference": root_reference,
                        "parent_span_reference": "",
                        "telemetry_type": "request",
                        "operation_name": "invoke_agent",
                        "timestamp": window_start,
                        "duration": 1250,
                        "success": "True",
                        "result_code": "200",
                        "tool_name": "",
                        "tool_call_reference": "",
                        "error_type": "",
                        "tool_ok": "",
                        "terminal_success": "True",
                        "terminal_output": "True",
                        "handled_error": "",
                        "output_messages_present": True,
                        "output_messages_nonempty": True,
                    },
                    {
                        "sequence": 2,
                        "span_reference": child_reference,
                        "parent_span_reference": root_reference,
                        "telemetry_type": "dependency",
                        "operation_name": "chat",
                        "timestamp": window_start,
                        "duration": 900,
                        "success": "True",
                        "result_code": "200",
                        "tool_name": "",
                        "tool_call_reference": "",
                        "error_type": "",
                        "tool_ok": "",
                        "terminal_success": "",
                        "terminal_output": "True",
                        "handled_error": "",
                        "output_messages_present": False,
                        "output_messages_nonempty": False,
                    },
                ],
            }
        ],
    }
    return sanitize_collected_trace_evidence(
        collected,
        role=role,
        attempt_index=index,
        runtime_agent_name=name,
        runtime_agent_version="1",
        window_start=window_start,
        window_end=window_end,
        endpoint={
            "request_count": 1,
            "response_count": 1,
            "usable_response_count": 1,
            "terminal_output_count": 1,
        },
        operation_ids=(f"{index:032x}",),
    )


def _attempt(role: str, index: int = 1) -> dict:
    return {
        "index": index,
        "conversation_reference": content_hash(
            {"role": role, "attempt": index, "conversation": True}
        ),
        "session_reference": content_hash(
            {"role": role, "attempt": index, "session": True}
        ),
        "operation_references": [
            content_hash({"role": role, "attempt": index, "operation": True})
        ],
        "mechanical_evidence": _mechanical(role, index),
        "complete": True,
        "defect_observed": None,
        "expected_observation_pass": False,
        "review_conclusion": "inconclusive",
        "judge_input_digest": None,
        "judge_output_digest": None,
        "error_code": None,
    }


def _package(kind: str = "issue") -> dict:
    role = "issue" if kind == "issue" else "baseline"
    mode = "model_mediated" if kind == "issue" else "baseline"
    scenario = _scenario(mode)
    n = scenario["n"]
    return build_judge_input(
        binding=_binding(),
        authority=_authority(kind),
        scenario=scenario,
        subject_attempts=[_attempt(role, index) for index in range(1, n + 1)],
        paired_v0_attempts=(
            [_attempt("paired_v0", index) for index in range(1, n + 1)]
            if kind == "issue"
            else []
        ),
        issue=_issue() if kind == "issue" else None,
        baseline_output_messages="present",
    )


def _output(
    package: dict,
    *,
    subject: str,
    paired_v0: str | None,
) -> dict:
    def conclusion(verdict: str, index: int) -> dict:
        return {
            "attempt_index": index,
            "verdict": verdict,
            "citations": [
                f"attempt-{index:02d}-endpoint",
                f"attempt-{index:02d}-trace-001",
            ],
            "reasoning": "Independent synthetic endpoint and trace evidence supports this result.",
        }

    baseline = package["authority_kind"] == "baseline"
    indexes = range(1, package["attempt_count"] + 1)
    return stamp_judge_output(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-judge-output",
            "model": JUDGE_MODEL,
            "prompt_version": JUDGE_PROMPT_VERSION,
            "prompt_digest": judge_prompt_digest(),
            "input_digest": package["input_digest"],
            "baseline_review": (
                {
                    "verdict": "healthy" if subject == "observed" else "inconclusive",
                    "citations": [
                        citation
                        for index in indexes
                        for citation in (
                            f"attempt-{index:02d}-endpoint",
                            f"attempt-{index:02d}-trace-001",
                        )
                    ],
                    "reasoning": "All synthetic baseline attempts have endpoint and trace evidence.",
                }
                if baseline
                else None
            ),
            "issue_reviews": (
                [conclusion(subject, index) for index in indexes]
                if not baseline
                else None
            ),
            "paired_v0_reviews": (
                [conclusion(paired_v0, index) for index in indexes]
                if paired_v0 is not None
                else None
            ),
            "output_digest": "",
        }
    )


def test_clear_issue_and_paired_v0_contrast_is_reviewed(tmp_path) -> None:
    class Client:
        @staticmethod
        def review(package, **_kwargs):
            return _output(
                package,
                subject="observed",
                paired_v0="not_observed",
            )

    judge = ValidationJudge(
        client=Client(),
        issues={"issue-001": _issue()},
        baseline_output_messages={"weather-agent": "present"},
        **_binding(),
        maximum_concurrency=2,
        root=tmp_path,
    )
    scenario = _scenario()
    subject, paired = judge.review_scenario(
        authority=_authority(),
        scenario=scenario,
        subject_attempts=[
            _attempt("issue", index) for index in range(1, 8)
        ],
        paired_v0_attempts=[
            _attempt("paired_v0", index) for index in range(1, 8)
        ],
    )
    assert all(item["review_conclusion"] == "observed" for item in subject)
    assert all(item["review_conclusion"] == "not_observed" for item in paired)
    assert all(item["judge_output_digest"] for item in [*subject, *paired])


def test_baseline_health_is_reviewed_without_paired_control(tmp_path) -> None:
    class Client:
        @staticmethod
        def review(package, **_kwargs):
            return _output(package, subject="observed", paired_v0=None)

    judge = ValidationJudge(
        client=Client(),
        issues={},
        baseline_output_messages={"weather-agent": "present"},
        **_binding(),
        maximum_concurrency=2,
        root=tmp_path,
    )
    subject, paired = judge.review_scenario(
        authority=_authority("baseline"),
        scenario=_scenario("baseline"),
        subject_attempts=[
            _attempt("baseline", index) for index in range(1, 6)
        ],
        paired_v0_attempts=[],
    )
    assert paired == []
    assert summarize_reviewed_scenario(
        authority_kind="baseline",
        validation_mode="baseline",
        subject_attempts=subject,
        paired_v0_attempts=[],
    )["pass"] is True


def test_output_messages_expectation_inherits_and_paired_v0_is_present() -> None:
    package = _package()
    assert package["reviewer_context"]["output_messages_expectation"] == {
        "subject": "present",
        "paired_v0": "present",
    }

    scenario = _scenario()
    issue = {**_issue(), "trace_contract": {"output_messages_expectation": "absent"}}
    overridden = build_judge_input(
        binding=_binding(),
        authority=_authority(),
        scenario=scenario,
        subject_attempts=[
            _attempt("issue", index) for index in range(1, 8)
        ],
        paired_v0_attempts=[
            _attempt("paired_v0", index) for index in range(1, 8)
        ],
        issue=issue,
        baseline_output_messages="present",
    )
    assert overridden["reviewer_context"]["output_messages_expectation"] == {
        "subject": "absent",
        "paired_v0": "present",
    }


def test_output_messages_structure_is_top_level_only() -> None:
    evidence = _mechanical("baseline")
    root, child = evidence["trace_graph"]["nodes"]
    assert root["operation_name"] == "invoke_agent"
    assert root["output_messages_present"] is True
    assert root["output_messages_nonempty"] is True
    assert child["output_messages_present"] is None
    assert child["output_messages_nonempty"] is None


def test_missing_top_level_output_messages_reaches_aggregate_judge(
    tmp_path,
) -> None:
    attempts = [_attempt("baseline", index) for index in range(1, 6)]
    root = attempts[0]["mechanical_evidence"]["trace_graph"]["nodes"][0]
    root["output_messages_present"] = False
    root["output_messages_nonempty"] = False
    evidence = attempts[0]["mechanical_evidence"]
    evidence["evidence_digest"] = content_hash(
        {key: value for key, value in evidence.items() if key != "evidence_digest"}
    )

    class Client:
        @staticmethod
        def review(package, **_kwargs):
            assert (
                package["subject_evidence"][0]["trace_graph"]["nodes"][0][
                    "output_messages_present"
                ]
                is False
            )
            return _output(package, subject="inconclusive", paired_v0=None)

    judge = ValidationJudge(
        client=Client(),
        issues={},
        baseline_output_messages={"weather-agent": "present"},
        **_binding(),
        maximum_concurrency=1,
        root=tmp_path,
    )
    reviewed, _ = judge.review_scenario(
        authority=_authority("baseline"),
        scenario=_scenario("baseline"),
        subject_attempts=attempts,
        paired_v0_attempts=[],
    )
    assert all(item["review_conclusion"] == "inconclusive" for item in reviewed)


def test_missing_trace_evidence_is_inconclusive_without_model_call(tmp_path) -> None:
    reviewed = 0

    class Client:
        @staticmethod
        def review(package, **_kwargs):
            nonlocal reviewed
            reviewed += 1
            return _output(package, subject="observed", paired_v0=None)

    attempts = [_attempt("baseline", index) for index in range(1, 6)]
    attempts[2]["mechanical_evidence"]["trace_graph"]["nodes"] = []
    judge = ValidationJudge(
        client=Client(),
        issues={},
        baseline_output_messages={"weather-agent": "present"},
        **_binding(),
        maximum_concurrency=1,
        root=tmp_path,
    )
    subject, _ = judge.review_scenario(
        authority=_authority("baseline"),
        scenario=_scenario("baseline"),
        subject_attempts=attempts,
        paired_v0_attempts=[],
    )
    assert all(item["review_conclusion"] == "inconclusive" for item in subject)
    assert all(item["judge_output_digest"] is None for item in subject)
    assert reviewed == 0


def test_uncited_conclusion_is_rejected() -> None:
    package = _package()
    output = _output(
        package,
        subject="observed",
        paired_v0="not_observed",
    )
    output["issue_reviews"][0]["citations"] = [
        "attempt-01-endpoint",
        "attempt-01-trace-999",
    ]
    output = stamp_judge_output(output)
    with pytest.raises(ContractError, match="citations"):
        validate_judge_output(output, package)


def test_wrong_model_prompt_and_cycle_bindings_are_rejected() -> None:
    package = _package()
    output = _output(
        package,
        subject="observed",
        paired_v0="not_observed",
    )
    wrong_model = copy.deepcopy(output)
    wrong_model["model"] = "gpt-5.6-terra"
    wrong_model = stamp_judge_output(wrong_model)
    with pytest.raises(ContractError, match="schema error"):
        validate_judge_output(wrong_model, package)

    wrong_prompt = copy.deepcopy(package)
    wrong_prompt["prompt_digest"] = HASH
    wrong_prompt["input_digest"] = content_hash(
        {key: value for key, value in wrong_prompt.items() if key != "input_digest"}
    )
    with pytest.raises(ContractError, match="prompt digest"):
        validate_judge_input(wrong_prompt)

    with pytest.raises(ContractError, match="cycle binding"):
        validate_judge_input(
            package,
            expected_binding={"cycle_id": "validation-other-cycle"},
        )


def test_sanitized_package_schema_rejects_raw_trace_field() -> None:
    package = _package()
    package["subject_evidence"][0]["raw_trace"] = {"messages": ["not allowed"]}
    package["input_digest"] = content_hash(
        {key: value for key, value in package.items() if key != "input_digest"}
    )
    with pytest.raises(ContractError, match="schema error"):
        validate_judge_input(package)


@pytest.mark.parametrize(
    ("mode", "observed", "expected"),
    [
        ("deterministic", 5, True),
        ("deterministic", 4, False),
        ("model_mediated", 5, True),
        ("model_mediated", 4, False),
    ],
)
def test_reviewed_thresholds_are_exact(
    mode: str,
    observed: int,
    expected: bool,
) -> None:
    n = 7 if mode == "model_mediated" else 5
    subject = [
        {
            **_attempt("issue", index),
            "complete": True,
            "review_conclusion": "observed" if index <= observed else "not_observed",
        }
        for index in range(1, n + 1)
    ]
    paired = [
        {
            **_attempt("paired_v0", index),
            "complete": True,
            "review_conclusion": "not_observed",
        }
        for index in range(1, n + 1)
    ]
    assert summarize_reviewed_scenario(
        authority_kind="issue",
        validation_mode=mode,
        subject_attempts=subject,
        paired_v0_attempts=paired,
    )["pass"] is expected


def test_review_cannot_resample_model_mediated_attempts() -> None:
    with pytest.raises(ContractError, match="resample"):
        summarize_reviewed_scenario(
            authority_kind="issue",
            validation_mode="model_mediated",
            subject_attempts=[_attempt("issue", index) for index in range(1, 6)],
            paired_v0_attempts=[
                _attempt("paired_v0", index) for index in range(1, 6)
            ],
        )


def test_aggregate_review_is_single_call_and_attempts_remain_isolated(tmp_path) -> None:
    lock = threading.Lock()
    calls = 0

    class Client:
        @staticmethod
        def review(package, **_kwargs):
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.01)
            return _output(
                package,
                subject="observed",
                paired_v0="not_observed",
            )

    judge = ValidationJudge(
        client=Client(),
        issues={"issue-001": _issue()},
        baseline_output_messages={"weather-agent": "present"},
        **_binding(),
        maximum_concurrency=2,
        root=tmp_path,
    )
    subject, paired = judge.review_scenario(
        authority=_authority(),
        scenario=_scenario(),
        subject_attempts=[
            _attempt("issue", index) for index in range(1, 8)
        ],
        paired_v0_attempts=[
            _attempt("paired_v0", index) for index in range(1, 8)
        ],
    )
    assert calls == 1
    assert len({item["judge_input_digest"] for item in subject}) == 1
    assert {item["index"] for item in subject} == set(range(1, 8))
    assert {item["index"] for item in paired} == set(range(1, 8))
