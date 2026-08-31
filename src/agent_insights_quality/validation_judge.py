from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    content_hash,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation_lifecycle import (
    LocalRecord,
    validation_runtime_root,
)
from agent_insights_quality.validation_rules import validation_matrix

JUDGE_MODEL = "gpt-5.6-sol"
JUDGE_PROMPT_VERSION = "1.0.0"
JUDGE_PROMPT_PATH = (
    ROOT / "src" / "agent_insights_quality" / "prompts" / "test_agent_validation.md"
)
JUDGE_INPUT_SCHEMA_PATH = (
    ROOT / "schemas" / "test-agent-validation-judge-input.schema.json"
)
JUDGE_OUTPUT_SCHEMA_PATH = (
    ROOT / "schemas" / "test-agent-validation-judge-output.schema.json"
)
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class JudgeClient(Protocol):
    def review(
        self,
        package: Mapping[str, Any],
        *,
        prompt: str,
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class FoundryJudgeClient:
    def __init__(
        self,
        *,
        endpoint: str,
        request_json: Callable[..., Mapping[str, Any]],
        max_output_tokens: int = 1200,
    ) -> None:
        if not endpoint or max_output_tokens < 1:
            raise ContractError("Validation judge client configuration is invalid")
        self._endpoint = endpoint.rstrip("/")
        self._request_json = request_json
        self._max_output_tokens = max_output_tokens

    def review(
        self,
        package: Mapping[str, Any],
        *,
        prompt: str,
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        model_schema = copy.deepcopy(dict(output_schema))
        model_schema["required"].remove("output_digest")
        model_schema["properties"].pop("output_digest")
        response = self._request_json(
            "POST",
            f"{self._endpoint}/openai/v1/responses",
            {
                "model": JUDGE_MODEL,
                "store": False,
                "max_output_tokens": self._max_output_tokens,
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": json.dumps(
                                    package,
                                    sort_keys=True,
                                    ensure_ascii=True,
                                    separators=(",", ":"),
                                ),
                            }
                        ],
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "test_agent_validation_judge_output",
                        "strict": True,
                        "schema": model_schema,
                    }
                },
            },
            expected={200},
        )
        if response.get("model") != JUDGE_MODEL:
            raise ContractError("Validation judge response model identity is invalid")
        text = _response_text(response)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ContractError("Validation judge returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ContractError("Validation judge output must be an object")
        return value


class ValidationJudge:
    def __init__(
        self,
        *,
        client: JudgeClient,
        issues: Mapping[str, Mapping[str, Any]],
        baseline_output_messages: Mapping[str, str],
        repository: str,
        pr_number: int,
        cycle_id: str,
        commit_sha: str,
        validation_digest: str,
        runtime_topology_digest: str,
        maximum_concurrency: int,
        root: Path | None = None,
    ) -> None:
        if maximum_concurrency < 1 or maximum_concurrency > 4:
            raise ContractError(
                "Validation judge concurrency must be within the reviewed limit"
            )
        self._client = client
        self._issues = {key: dict(value) for key, value in issues.items()}
        self._baseline_output_messages = dict(baseline_output_messages)
        self._binding = {
            "repository": repository,
            "pr_number": pr_number,
            "cycle_id": cycle_id,
            "commit_sha": commit_sha,
            "validation_digest": validation_digest,
            "runtime_topology_digest": runtime_topology_digest,
        }
        self._maximum_concurrency = maximum_concurrency
        self._slots = threading.BoundedSemaphore(maximum_concurrency)
        self._root = root
        self._prompt = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
        self._prompt_digest = judge_prompt_digest()
        self._output_schema = read_json(JUDGE_OUTPUT_SCHEMA_PATH)

    def review_scenario(
        self,
        *,
        authority: Any,
        scenario: Mapping[str, Any],
        subject_attempts: Sequence[Mapping[str, Any]],
        paired_v0_attempts: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        n = int(scenario["n"])
        if len(subject_attempts) != n or (
            authority.authority_kind == "issue"
            and len(paired_v0_attempts) != n
        ):
            raise ContractError("Validation judge attempt inventory is incomplete")
        if authority.authority_kind == "baseline" and paired_v0_attempts:
            raise ContractError("Validation baseline cannot have paired v0 evidence")
        if not all(mechanical_attempt_complete(item) for item in subject_attempts) or (
            paired_v0_attempts
            and not all(
                mechanical_attempt_complete(item) for item in paired_v0_attempts
            )
        ):
            return (
                [
                    _inconclusive_attempt(
                        item,
                        "mechanical_evidence_incomplete",
                    )
                    for item in subject_attempts
                ],
                [
                    _inconclusive_attempt(
                        item,
                        "mechanical_evidence_incomplete",
                    )
                    for item in paired_v0_attempts
                ],
            )
        package = build_judge_input(
            binding=self._binding,
            authority=authority,
            scenario=scenario,
            subject_attempts=subject_attempts,
            paired_v0_attempts=paired_v0_attempts,
            issue=self._issues.get(authority.authority_id),
            baseline_output_messages=self._baseline_output_messages[
                authority.canonical_agent
            ],
        )
        input_record = persist_judge_input(package, root=self._root)
        try:
            with self._slots:
                output = stamp_judge_output(
                    self._client.review(
                        package,
                        prompt=self._prompt,
                        output_schema=self._output_schema,
                    )
                )
            validate_judge_output(output, package)
            output_record = persist_judge_output(
                output,
                package=package,
                root=self._root,
            )
        except (ContractError, OSError, RuntimeError):
            return (
                [
                    _inconclusive_attempt(
                        item,
                        "judge_review_failed",
                        input_digest=input_record.digest,
                    )
                    for item in subject_attempts
                ],
                [
                    _inconclusive_attempt(
                        item,
                        "judge_review_failed",
                        input_digest=input_record.digest,
                    )
                    for item in paired_v0_attempts
                ],
            )
        if authority.authority_kind == "baseline":
            baseline_verdict = output["baseline_review"]["verdict"]
            conclusion = (
                "observed" if baseline_verdict == "healthy" else "inconclusive"
            )
            return (
                [
                    _judged_attempt(
                        item,
                        conclusion,
                        input_record.digest,
                        output_record.digest,
                    )
                    for item in subject_attempts
                ],
                [],
            )
        return (
            [
                _judged_attempt(
                    item,
                    review["verdict"],
                    input_record.digest,
                    output_record.digest,
                )
                for item, review in zip(
                    subject_attempts,
                    output["issue_reviews"],
                    strict=True,
                )
            ],
            [
                _judged_attempt(
                    item,
                    review["verdict"],
                    input_record.digest,
                    output_record.digest,
                )
                for item, review in zip(
                    paired_v0_attempts,
                    output["paired_v0_reviews"],
                    strict=True,
                )
            ],
        )


def judge_prompt_digest() -> str:
    return content_hash(
        {
            "version": JUDGE_PROMPT_VERSION,
            "content": JUDGE_PROMPT_PATH.read_text(encoding="utf-8"),
        }
    )


def sanitize_collected_trace_evidence(
    value: Mapping[str, Any],
    *,
    role: str,
    attempt_index: int,
    runtime_agent_name: str,
    runtime_agent_version: str,
    window_start: str,
    window_end: str,
    endpoint: Mapping[str, Any],
    operation_ids: Sequence[str],
) -> dict[str, Any]:
    operations = value.get("operations")
    operation_count = value.get("operation_count")
    span_count = value.get("span_count")
    if (
        role not in {"baseline", "issue", "paired_v0"}
        or attempt_index < 1
        or attempt_index > 7
        or not isinstance(operations, list)
        or not operations
        or operation_count != len(operations)
        or operation_count != len(operation_ids)
        or not isinstance(span_count, int)
        or isinstance(span_count, bool)
        or span_count < 1
    ):
        raise ContractError("Collected validation trace package is incomplete")
    flattened: list[Mapping[str, Any]] = []
    for operation in operations:
        if (
            not isinstance(operation, Mapping)
            or not _HASH.fullmatch(str(operation.get("operation_reference") or ""))
            or not isinstance(operation.get("spans"), list)
            or not operation["spans"]
        ):
            raise ContractError("Collected validation trace operation is invalid")
        flattened.extend(operation["spans"])
    if len(flattened) != span_count:
        raise ContractError("Collected validation trace span count is invalid")

    prefix = f"attempt-{attempt_index:02d}"
    indexed = sorted(
        enumerate(flattened),
        key=lambda item: (
            _nonnegative_int(item[1].get("sequence"), "span sequence"),
            item[0],
        ),
    )
    citation_by_reference = {
        str(span["span_reference"]): f"{prefix}-trace-{position:03d}"
        for position, (_, span) in enumerate(indexed, start=1)
        if span.get("span_reference")
    }
    nodes: list[dict[str, Any]] = []
    root_count = 0
    for position, (_, span) in enumerate(indexed, start=1):
        if not isinstance(span, Mapping):
            raise ContractError("Collected validation trace span is invalid")
        parent_reference = str(span.get("parent_span_reference") or "")
        if parent_reference and parent_reference not in citation_by_reference:
            raise ContractError("Collected validation trace parent is missing")
        operation_name = _safe_name(span.get("operation_name"), "operation name")
        if operation_name not in {"invoke_agent", "execute_tool", "chat"}:
            raise ContractError("Collected validation trace operation is not allowlisted")
        top_level_invoke = operation_name == "invoke_agent" and not parent_reference
        if top_level_invoke:
            root_count += 1
            output_messages_present = span.get("output_messages_present")
            output_messages_nonempty = span.get("output_messages_nonempty")
            if not isinstance(output_messages_present, bool) or not isinstance(
                output_messages_nonempty,
                bool,
            ):
                raise ContractError(
                    "Collected invoke_agent output-message structure is missing"
                )
            if output_messages_nonempty and not output_messages_present:
                raise ContractError(
                    "Collected invoke_agent output-message structure is invalid"
                )
        else:
            output_messages_present = None
            output_messages_nonempty = None
        success = str(span.get("success") or "").casefold()
        tool_ok = str(span.get("tool_ok") or "").casefold()
        terminal_success = str(span.get("terminal_success") or "").casefold()
        error_present = bool(str(span.get("error_type") or ""))
        status_class = (
            "error"
            if success == "false" or error_present
            else "success"
            if success == "true"
            else "unset"
        )
        result_class = (
            "mixed"
            if tool_ok == "false" and terminal_success == "true"
            else "error"
            if tool_ok == "false"
            else "success"
            if tool_ok == "true" or terminal_success == "true"
            else "none"
        )
        duration = _finite_number(span.get("duration"), "span duration")
        tool_name = str(span.get("tool_name") or "")
        result_code = str(span.get("result_code") or "")
        nodes.append(
            {
                "citation": f"{prefix}-trace-{position:03d}",
                "parent": (
                    citation_by_reference[parent_reference]
                    if parent_reference
                    else None
                ),
                "order": position,
                "operation_name": operation_name,
                "duration_bucket": _duration_bucket(max(0.0, duration)),
                "tool_name": (
                    _safe_name(tool_name, "tool name") if tool_name else None
                ),
                "status_class": status_class,
                "result_class": result_class,
                "error_class": "error" if error_present else None,
                "output_messages_present": output_messages_present,
                "output_messages_nonempty": output_messages_nonempty,
                "structure": {
                    "input_present": bool(
                        str(span.get("tool_call_reference") or "")
                    ),
                    "output_present": str(
                        span.get("terminal_output") or ""
                    ).casefold()
                    == "true",
                    "result_present": bool(result_code or tool_ok),
                    "error_present": error_present,
                },
            }
        )
    if root_count < 1:
        raise ContractError("Collected validation trace has no invoke_agent root")
    request_count = _nonnegative_int(endpoint.get("request_count"), "request count")
    response_count = _nonnegative_int(endpoint.get("response_count"), "response count")
    usable_count = _nonnegative_int(
        endpoint.get("usable_response_count"),
        "usable response count",
    )
    terminal_count = _nonnegative_int(
        endpoint.get("terminal_output_count"),
        "terminal output count",
    )
    evidence = {
        "role": role,
        "attempt_index": attempt_index,
        "runtime_agent_reference": content_hash(
            {"runtime_agent_name": runtime_agent_name}
        ),
        "runtime_version_reference": content_hash(
            {"runtime_agent_version": runtime_agent_version}
        ),
        "event_range_reference": content_hash(
            {"window_start": window_start, "window_end": window_end}
        ),
        "endpoint": {
            "citation": f"{prefix}-endpoint",
            "request_count": request_count,
            "response_count": response_count,
            "usable_response_count": usable_count,
            "terminal_output_count": terminal_count,
        },
        "trace_graph": {"root_count": root_count, "nodes": nodes},
        "evidence_digest": "",
    }
    evidence["evidence_digest"] = _digest_without(evidence, "evidence_digest")
    _validate_mechanical_evidence(evidence)
    return evidence


def build_judge_input(
    *,
    binding: Mapping[str, Any],
    authority: Any,
    scenario: Mapping[str, Any],
    subject_attempts: Sequence[Mapping[str, Any]],
    paired_v0_attempts: Sequence[Mapping[str, Any]],
    issue: Mapping[str, Any] | None,
    baseline_output_messages: str,
) -> dict[str, Any]:
    n = int(scenario["n"])
    if len(subject_attempts) != n or (
        authority.authority_kind == "issue" and len(paired_v0_attempts) != n
    ):
        raise ContractError("Validation judge cannot resample attempt evidence")
    subject_evidence = [
        dict(item["mechanical_evidence"]) for item in subject_attempts
    ]
    paired_evidence = (
        [dict(item["mechanical_evidence"]) for item in paired_v0_attempts]
        if paired_v0_attempts
        else None
    )
    for evidence in subject_evidence:
        _validate_mechanical_evidence(evidence)
    for evidence in paired_evidence or []:
        _validate_mechanical_evidence(evidence)
    if authority.authority_kind == "issue" and issue is None:
        raise ContractError("Validation judge issue context is missing")
    if authority.authority_kind == "baseline" and issue is not None:
        raise ContractError("Validation judge baseline cannot contain issue context")
    issue_override = (
        issue.get("trace_contract", {}).get("output_messages_expectation")
        if issue is not None
        else None
    )
    subject_output_messages = issue_override or baseline_output_messages
    if subject_output_messages not in {"present", "absent", "not_applicable"}:
        raise ContractError(
            "Validation judge output-message expectation is not reviewed"
        )
    package = {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-judge-input",
        **dict(binding),
        "authority_id": authority.authority_id,
        "authority_kind": authority.authority_kind,
        "canonical_agent": authority.canonical_agent,
        "logical_version": authority.logical_version,
        "scenario_id": scenario["id"],
        "execution_digest": scenario["execution_digest"],
        "validation_mode": scenario["validation_mode"],
        "attempt_count": n,
        "model": JUDGE_MODEL,
        "prompt_version": JUDGE_PROMPT_VERSION,
        "prompt_digest": judge_prompt_digest(),
        "reviewer_context": {
            "expected_issue": (
                {
                    "issue_id": issue["id"],
                    "title": issue["title"],
                    "root_cause": issue["root_cause"],
                    "category": issue["category"],
                    "severity": issue["severity"],
                    "expected_fix": issue["expected_fix"],
                }
                if issue is not None
                else None
            ),
            "output_messages_expectation": {
                "subject": subject_output_messages,
                "paired_v0": (
                    "present" if authority.authority_kind == "issue" else None
                ),
            },
            "traffic_contract": [
                _traffic_context(attempt) for attempt in scenario["attempts"]
            ],
        },
        "subject_evidence": subject_evidence,
        "paired_v0_evidence": paired_evidence,
        "input_digest": "",
    }
    package["input_digest"] = _digest_without(package, "input_digest")
    validate_judge_input(package)
    return package


def validate_judge_input(
    value: Mapping[str, Any],
    *,
    expected_binding: Mapping[str, Any] | None = None,
) -> None:
    _validate_schema(JUDGE_INPUT_SCHEMA_PATH, value, "judge input")
    if value["prompt_digest"] != judge_prompt_digest():
        raise ContractError("Validation judge input prompt digest is stale")
    if value["input_digest"] != _digest_without(value, "input_digest"):
        raise ContractError("Validation judge input digest is stale")
    if expected_binding is not None and any(
        value.get(key) != expected for key, expected in expected_binding.items()
    ):
        raise ContractError("Validation judge input cycle binding is stale")
    subject = value["subject_evidence"]
    for evidence in subject:
        _validate_mechanical_evidence(evidence)
    paired = value["paired_v0_evidence"]
    if paired is not None:
        for evidence in paired:
            _validate_mechanical_evidence(evidence)
    expected_indexes = list(range(1, value["attempt_count"] + 1))
    if (
        len(subject) != value["attempt_count"]
        or [item["attempt_index"] for item in subject] != expected_indexes
        or (
            paired is not None
            and (
                len(paired) != value["attempt_count"]
                or [item["attempt_index"] for item in paired] != expected_indexes
            )
        )
    ):
        raise ContractError("Validation judge input attempt inventory is invalid")
    if value["authority_kind"] == "baseline":
        if (
            any(item["role"] != "baseline" for item in subject)
            or value["reviewer_context"]["expected_issue"] is not None
        ):
            raise ContractError("Validation baseline judge context is invalid")
    elif (
        any(item["role"] != "issue" for item in subject)
        or paired is None
        or any(item["role"] != "paired_v0" for item in paired)
        or value["reviewer_context"]["expected_issue"]["issue_id"]
        != value["authority_id"]
    ):
        raise ContractError("Validation issue judge context is invalid")


def stamp_judge_output(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["output_digest"] = _digest_without(result, "output_digest")
    return result


def validate_judge_output(
    value: Mapping[str, Any],
    package: Mapping[str, Any],
) -> None:
    validate_judge_input(package)
    _validate_schema(JUDGE_OUTPUT_SCHEMA_PATH, value, "judge output")
    for field in ("model", "prompt_version", "prompt_digest", "input_digest"):
        expected = (
            package[field]
            if field != "input_digest"
            else package["input_digest"]
        )
        if value[field] != expected:
            raise ContractError(f"Validation judge output {field} is stale")
    if value["output_digest"] != _digest_without(value, "output_digest"):
        raise ContractError("Validation judge output digest is stale")
    baseline = package["authority_kind"] == "baseline"
    if baseline:
        if (
            value["baseline_review"] is None
            or value["issue_reviews"] is not None
            or value["paired_v0_reviews"] is not None
        ):
            raise ContractError("Validation judge baseline output shape is invalid")
        _validate_baseline_citations(
            value["baseline_review"],
            package["subject_evidence"],
        )
        return
    if (
        value["baseline_review"] is not None
        or not isinstance(value["issue_reviews"], list)
        or not isinstance(value["paired_v0_reviews"], list)
        or len(value["issue_reviews"]) != package["attempt_count"]
        or len(value["paired_v0_reviews"]) != package["attempt_count"]
    ):
        raise ContractError("Validation judge issue output shape is invalid")
    for reviews, evidence_items in (
        (value["issue_reviews"], package["subject_evidence"]),
        (value["paired_v0_reviews"], package["paired_v0_evidence"]),
    ):
        if [item["attempt_index"] for item in reviews] != list(
            range(1, package["attempt_count"] + 1)
        ):
            raise ContractError("Validation judge output attempt order is invalid")
        for conclusion, evidence in zip(reviews, evidence_items, strict=True):
            _validate_conclusion_citations(conclusion, evidence)


def persist_judge_input(
    value: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> LocalRecord:
    validate_judge_input(value)
    return _persist_judge_artifact(value, "inputs", "input_digest", root)


def persist_judge_output(
    value: Mapping[str, Any],
    *,
    package: Mapping[str, Any],
    root: Path | None = None,
) -> LocalRecord:
    validate_judge_output(value, package)
    return _persist_judge_artifact(value, "outputs", "output_digest", root)


def mechanical_attempt_complete(value: Mapping[str, Any]) -> bool:
    evidence = value.get("mechanical_evidence")
    if not isinstance(evidence, Mapping):
        return False
    try:
        _validate_mechanical_evidence(evidence)
    except ContractError:
        return False
    endpoint = evidence["endpoint"]
    graph = evidence["trace_graph"]
    request_count = endpoint["request_count"]
    return (
        request_count > 0
        and endpoint["response_count"] == request_count
        and endpoint["usable_response_count"] == request_count
        and graph["root_count"] >= 1
    )


def aggregate_judge_digests(
    authorities: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    inputs: set[str] = set()
    outputs: set[str] = set()
    for authority in authorities:
        for scenario in authority["scenarios"]:
            for attempt in [
                *scenario["issue_attempts"],
                *scenario["v0_attempts"],
            ]:
                input_digest = attempt.get("judge_input_digest")
                output_digest = attempt.get("judge_output_digest")
                if input_digest is not None:
                    inputs.add(str(input_digest))
                if output_digest is not None:
                    outputs.add(str(output_digest))
    return content_hash(sorted(inputs)), content_hash(sorted(outputs))


def validate_mechanical_evidence(value: Mapping[str, Any]) -> None:
    _validate_mechanical_evidence(value)


def summarize_reviewed_scenario(
    *,
    authority_kind: str,
    validation_mode: str,
    subject_attempts: Sequence[Mapping[str, Any]],
    paired_v0_attempts: Sequence[Mapping[str, Any]],
) -> dict[str, int | bool]:
    n, k = validation_matrix(validation_mode)
    if len(subject_attempts) != n:
        raise ContractError("Validation judge cannot resample subject attempts")
    if authority_kind == "baseline":
        if validation_mode != "baseline" or paired_v0_attempts:
            raise ContractError("Validation judge baseline attempt shape is invalid")
    elif (
        authority_kind != "issue"
        or validation_mode == "baseline"
        or len(paired_v0_attempts) != n
    ):
        raise ContractError("Validation judge cannot resample paired v0 attempts")
    complete_count = sum(item.get("complete") is True for item in subject_attempts)
    observed = sum(
        item.get("review_conclusion") == "observed" for item in subject_attempts
    )
    if authority_kind == "baseline":
        passed = complete_count == n and observed == n
    else:
        control_complete = sum(
            item.get("complete") is True for item in paired_v0_attempts
        )
        control_observed = sum(
            item.get("review_conclusion") == "observed"
            for item in paired_v0_attempts
        )
        control_inconclusive = any(
            item.get("review_conclusion") == "inconclusive"
            for item in paired_v0_attempts
        )
        passed = (
            complete_count == n
            and observed >= k
            and control_complete == n
            and control_observed == 0
            and not control_inconclusive
            and all(
                item.get("review_conclusion") == "not_observed"
                for item in paired_v0_attempts
            )
        )
    return {
        "n": n,
        "k": k,
        "complete_count": complete_count,
        "observed": observed,
        "pass": passed,
    }


def _traffic_context(attempt: Mapping[str, Any]) -> dict[str, Any]:
    def step_context(step: Mapping[str, Any]) -> dict[str, Any]:
        expected = step["expected"]
        return {
            "step_id": step["id"],
            "request_digest": content_hash(step["request"]),
            "semantic_expectations": sorted(expected["semantic_assertions"]),
            "trace_expectations": sorted(
                str(item["name"]) for item in expected["trace_assertions"]
            ),
        }

    setup = [step_context(item) for item in attempt["setup_steps"]]
    probe = [step_context(item) for item in attempt["probe_steps"]]
    return {
        "attempt_index": attempt["index"],
        "request_digest": content_hash(
            {
                "parameters": attempt["parameters"],
                "setup_steps": setup,
                "probe_steps": probe,
            }
        ),
        "setup_steps": setup,
        "probe_steps": probe,
    }


def _validate_mechanical_evidence(value: Mapping[str, Any]) -> None:
    endpoint = value.get("endpoint")
    graph = value.get("trace_graph")
    if not isinstance(endpoint, Mapping) or not isinstance(graph, Mapping):
        raise ContractError("Validation judge mechanical evidence is invalid")
    if value.get("evidence_digest") != _digest_without(value, "evidence_digest"):
        raise ContractError("Validation judge mechanical evidence digest is stale")
    request_count = endpoint.get("request_count")
    nodes = graph.get("nodes")
    root_count = graph.get("root_count")
    if (
        not isinstance(request_count, int)
        or isinstance(request_count, bool)
        or request_count < 1
        or not isinstance(nodes, list)
        or not nodes
        or not isinstance(root_count, int)
        or isinstance(root_count, bool)
        or root_count < 1
    ):
        raise ContractError("Validation judge mechanical trace inventory is invalid")
    citations = [node.get("citation") for node in nodes if isinstance(node, Mapping)]
    if len(citations) != len(nodes) or len(citations) != len(set(citations)):
        raise ContractError("Validation judge trace citations are invalid")
    seen: set[str] = set()
    roots = 0
    for expected_order, node in enumerate(nodes, start=1):
        parent = node.get("parent")
        if node.get("order") != expected_order or (
            parent is not None and parent not in seen
        ):
            raise ContractError("Validation judge trace graph order is invalid")
        if parent is None:
            roots += 1
            if node.get("operation_name") != "invoke_agent":
                raise ContractError("Validation judge trace root is not invoke_agent")
            if not isinstance(
                node.get("output_messages_present"),
                bool,
            ) or not isinstance(node.get("output_messages_nonempty"), bool):
                raise ContractError(
                    "Validation judge invoke_agent output-message structure is invalid"
                )
        elif (
            node.get("output_messages_present") is not None
            or node.get("output_messages_nonempty") is not None
        ):
            raise ContractError(
                "Validation judge child span cannot carry output-message structure"
            )
        seen.add(str(node["citation"]))
    if roots != root_count or roots < 1:
        raise ContractError("Validation judge trace root count is invalid")


def _validate_conclusion_citations(
    conclusion: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    valid = {
        evidence["endpoint"]["citation"],
        *(node["citation"] for node in evidence["trace_graph"]["nodes"]),
    }
    citations = set(conclusion["citations"])
    if (
        not citations.issubset(valid)
        or evidence["endpoint"]["citation"] not in citations
        or not any("-trace-" in item for item in citations)
    ):
        raise ContractError(
            "Validation judge conclusion lacks independent endpoint and trace citations"
        )


def _validate_baseline_citations(
    conclusion: Mapping[str, Any],
    evidence_items: Sequence[Mapping[str, Any]],
) -> None:
    citations = set(conclusion["citations"])
    valid = {
        citation
        for evidence in evidence_items
        for citation in [
            evidence["endpoint"]["citation"],
            *(node["citation"] for node in evidence["trace_graph"]["nodes"]),
        ]
    }
    if not citations.issubset(valid):
        raise ContractError("Validation judge baseline citations are invalid")
    for evidence in evidence_items:
        if evidence["endpoint"]["citation"] not in citations or not any(
            node["citation"] in citations
            for node in evidence["trace_graph"]["nodes"]
        ):
            raise ContractError(
                "Validation judge baseline lacks cited endpoint and trace evidence"
            )


def _persist_judge_artifact(
    value: Mapping[str, Any],
    folder: str,
    digest_field: str,
    root: Path | None,
) -> LocalRecord:
    digest = str(value[digest_field])
    base = (root or validation_runtime_root() / "judge").resolve()
    private = validation_runtime_root().resolve()
    if root is None and not base.is_relative_to(private):
        raise ContractError("Validation judge artifacts must remain private")
    path = base / folder / f"{digest.removeprefix('sha256:')}.json"
    immutable_json(path, dict(value))
    persisted = read_json(path)
    if persisted.get(digest_field) != digest:
        raise ContractError("Immutable validation judge artifact digest changed")
    return LocalRecord(path=path, value=persisted, digest=digest)


def _judged_attempt(
    value: Mapping[str, Any],
    conclusion: str,
    input_digest: str,
    output_digest: str,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    role = result.get("mechanical_evidence", {}).get("role")
    expected = (
        conclusion == "not_observed"
        if role == "paired_v0"
        else conclusion == "observed"
    )
    result.update(
        {
            "complete": conclusion != "inconclusive",
            "defect_observed": (
                True
                if conclusion == "observed"
                else False
                if conclusion == "not_observed"
                else None
            ),
            "expected_observation_pass": expected,
            "review_conclusion": conclusion,
            "judge_input_digest": input_digest,
            "judge_output_digest": output_digest,
            "error_code": (
                None if conclusion != "inconclusive" else "judge_inconclusive"
            ),
        }
    )
    return result


def _inconclusive_attempt(
    value: Mapping[str, Any] | None,
    error_code: str,
    *,
    input_digest: str | None = None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    result = copy.deepcopy(dict(value))
    result.update(
        {
            "complete": False,
            "defect_observed": None,
            "expected_observation_pass": False,
            "review_conclusion": "inconclusive",
            "judge_input_digest": input_digest,
            "judge_output_digest": None,
            "error_code": error_code,
        }
    )
    return result


def _validate_schema(
    path: Path,
    value: Mapping[str, Any],
    label: str,
) -> None:
    errors = sorted(
        Draft202012Validator(
            read_json(path),
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Test Agent Validation {label} schema error at {location}: "
            f"{error.message}"
        )


def _response_text(response: Mapping[str, Any]) -> str:
    texts = [
        content.get("text")
        for item in response.get("output", [])
        if isinstance(item, Mapping) and item.get("type") == "message"
        for content in item.get("content", [])
        if isinstance(content, Mapping) and content.get("type") == "output_text"
    ]
    if len(texts) != 1 or not isinstance(texts[0], str) or not texts[0]:
        raise ContractError("Validation judge response has no single output JSON")
    return texts[0]


def _digest_without(value: Mapping[str, Any], field: str) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop(field, None)
    return content_hash(payload)


def _duration_bucket(value: float) -> str:
    if value < 100:
        return "under_100ms"
    if value < 1000:
        return "100ms_to_1s"
    if value < 10000:
        return "1s_to_10s"
    if value < 60000:
        return "10s_to_60s"
    return "over_60s"


def _safe_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
        raise ContractError(f"Collected trace {label} is not public-safe")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"Collected trace {label} is invalid")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"Collected endpoint {label} is invalid")
    return value


def _finite_number(value: Any, label: str) -> float:
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        value = total_seconds() * 1000
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"Collected trace {label} is invalid")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise ContractError(f"Collected trace {label} is invalid")
    return result
