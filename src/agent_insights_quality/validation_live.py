from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from agent_insights_quality.live import (
    LiveRuntime,
    _canonical_output_messages_expectation_passes,
    _normalize_fixture,
    _TELEMETRY_TRANSIENT_ERRORS,
)
from agent_insights_quality.models import InvocationEvidence
from agent_insights_quality.util import ContractError, SharedRuntimeError, content_hash
from agent_insights_quality.validation_quota import (
    EndpointCost,
    ValidationScheduler,
)
from agent_insights_quality.validation_runtime import DeployedRuntime

_POST_RESPONSE_TELEMETRY_ERRORS = (
    ContractError,
    OSError,
    RuntimeError,
    *_TELEMETRY_TRANSIENT_ERRORS,
)


class PostResponseTelemetryError(ContractError):
    request_accepted = True
    recoverable_issue_execution = True

    def __init__(self, error: BaseException) -> None:
        super().__init__("Post-response telemetry verification failed")
        self.code = str(getattr(error, "code", "") or type(error).__name__)
        for field in (
            "matched_reference_count",
            "expected_reference_count",
            "missing_reference_count",
        ):
            if hasattr(error, field):
                setattr(self, field, getattr(error, field))


class FoundryScenarioAttemptRunner:
    def __init__(
        self,
        runtime: LiveRuntime,
        *,
        endpoint_costs: Mapping[str, EndpointCost],
        stabilization_seconds: int,
        record_resource: Callable[[dict[str, Any]], None],
        record_duration: Callable[[str, float], None] = lambda _stage, _value: None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if stabilization_seconds <= 0:
            raise ContractError("Validation telemetry stabilization must be positive")
        self._runtime = runtime
        self._endpoint_costs = dict(endpoint_costs)
        self._stabilization_seconds = stabilization_seconds
        self._record_resource = record_resource
        self._record_duration = record_duration
        self._now = now

    def prepare_hosted_routes(self, targets: list[DeployedRuntime]) -> None:
        prepared: set[str] = set()
        for target in targets:
            if (
                target.runtime_kind not in {"hosted_code", "hosted_custom_container"}
                or target.runtime_agent_name in prepared
            ):
                continue
            self._runtime._activate_hosted_version(
                target.runtime_agent_name,
                target.runtime_agent_version,
            )
            prepared.add(target.runtime_agent_name)

    def invoke(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        scheduler: ValidationScheduler,
    ) -> dict[str, Any]:
        with scheduler.runtime_attempt(target.authority_id):
            return self._invoke(
                target=target,
                executing_authority_id=executing_authority_id,
                conversation_role=conversation_role,
                scenario=scenario,
                attempt=attempt,
                scheduler=scheduler,
            )

    def _invoke(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        scheduler: ValidationScheduler,
    ) -> dict[str, Any]:
        if (
            not executing_authority_id
            or conversation_role not in {"baseline", "issue", "paired_v0"}
        ):
            raise ContractError("Validation attempt execution identity is invalid")
        cost = self._endpoint_costs.get(executing_authority_id)
        if cost is None:
            raise ContractError(
                f"Validation endpoint cost is missing for {executing_authority_id}"
            )
        execution_scope = {
            "executing_authority_id": executing_authority_id,
            "target_authority_id": target.authority_id,
            "conversation_role": conversation_role,
            "scenario_id": scenario["id"],
            "conversation_group": attempt["conversation_group"],
            "attempt": attempt["index"],
        }
        raw_steps = [
            *[("setup", item) for item in attempt["setup_steps"]],
            *[("probe", item) for item in attempt["probe_steps"]],
        ]
        fixtures = [
            _normalize_fixture(
                {
                    "id": step["id"],
                    "request": step["request"],
                    "expected": {
                        "http_status": step["expected"].get("http_status", 200),
                        "semantic_assertions": {},
                        "trace_assertions": [],
                    },
                }
            )
            for _, step in raw_steps
        ]
        started = self._now().astimezone(UTC)
        endpoint_started = time.monotonic()
        response_references: list[str] = []
        usable_results: list[bool] = []
        session_id: str | None = None
        previous_response_id: str | None = None
        if target.runtime_kind == "prompt":
            for fixture_index, fixture in enumerate(fixtures, start=1):
                intent_reference = content_hash(
                    {
                        "authority_id": target.authority_id,
                        "kind": "stored_response",
                        "execution_scope": execution_scope,
                        "step": fixture_index,
                    }
                )
                self._record_resource(
                    {
                        "state": "create_intent",
                        "kind": "stored_response",
                        "intent_reference": intent_reference,
                        "deterministic_name": (
                            f"{target.runtime_agent_name}-"
                            f"{conversation_role}-{attempt['index']}-{fixture_index}"
                        ),
                        "authority_id": target.authority_id,
                        "parent_id": target.provider_agent_id,
                        "runtime_kind": target.runtime_kind,
                        "discovery_key": (
                            f"{target.runtime_agent_name}|{intent_reference}"
                        ),
                    }
                )
                scheduler.acquire_request(cost)
                with _observe_rate_limit(self._runtime, scheduler):
                    try:
                        result = self._runtime._invoke_prompt(
                            target.runtime_agent_name,
                            target.runtime_agent_version,
                            fixture,
                            0,
                            previous_response_id,
                            include_seed_metadata=False,
                            validation_intent_reference=intent_reference,
                        )
                    except ContractError:
                        self._record_resource(
                            {
                                "state": "ambiguous_create",
                                "kind": "stored_response",
                                "intent_reference": intent_reference,
                                "deterministic_name": (
                                    f"{target.runtime_agent_name}-"
                                    f"{conversation_role}-"
                                    f"{attempt['index']}-{fixture_index}"
                                ),
                                "authority_id": target.authority_id,
                                "parent_id": target.provider_agent_id,
                                "runtime_kind": target.runtime_kind,
                                "discovery_key": (
                                    f"{target.runtime_agent_name}|{intent_reference}"
                                ),
                            }
                        )
                        raise
                    (
                        response_ids,
                        usable,
                        _,
                        _,
                        _,
                        function_call_count,
                        _,
                        _,
                    ) = result
                if function_call_count:
                    raise ContractError(
                        "Prompt validation emitted an unsupported function call"
                    )
                previous_response_id = response_ids[-1]
                response_references.extend(response_ids)
                usable_results.append(usable)
                for response_id in response_ids:
                    self._record_resource(
                        {
                            "state": "created",
                            "kind": "stored_response",
                            "intent_reference": intent_reference,
                            "provider_id": response_id,
                            "deterministic_name": response_id,
                            "authority_id": target.authority_id,
                            "parent_id": target.provider_agent_id,
                        }
                    )
        elif target.runtime_kind in {"hosted_code", "hosted_custom_container"}:
            self._runtime._activate_hosted_version(
                target.runtime_agent_name,
                target.runtime_agent_version,
                refresh_route=True,
            )
            session_intent = content_hash(
                {
                    "authority_id": target.authority_id,
                    "kind": "session",
                    "execution_scope": execution_scope,
                }
            )
            self._record_resource(
                {
                    "state": "create_intent",
                    "kind": "session",
                    "intent_reference": session_intent,
                    "deterministic_name": (
                        f"{target.runtime_agent_name}-"
                        f"{conversation_role}-session-{attempt['index']}"
                    ),
                    "authority_id": target.authority_id,
                    "parent_id": target.provider_agent_id,
                    "runtime_kind": target.runtime_kind,
                    "discovery_key": (
                        f"{target.runtime_agent_name}|{session_intent}"
                    ),
                }
            )
            try:
                session_id = self._runtime._create_hosted_session(
                    target.runtime_agent_name,
                    target.runtime_agent_version,
                    validation_intent_reference=session_intent,
                )
            except ContractError:
                self._record_resource(
                    {
                        "state": "ambiguous_create",
                        "kind": "session",
                        "intent_reference": session_intent,
                        "deterministic_name": (
                            f"{target.runtime_agent_name}-"
                            f"{conversation_role}-session-{attempt['index']}"
                        ),
                        "authority_id": target.authority_id,
                        "parent_id": target.provider_agent_id,
                    }
                )
                raise
            self._record_resource(
                {
                    "state": "created",
                    "kind": "session",
                    "intent_reference": session_intent,
                    "provider_id": session_id,
                    "deterministic_name": session_id,
                    "authority_id": target.authority_id,
                    "parent_id": target.provider_agent_id,
                }
            )
            for fixture_index, fixture in enumerate(fixtures, start=1):
                response_intent = content_hash(
                    {
                        "authority_id": target.authority_id,
                        "kind": "stored_response",
                        "execution_scope": execution_scope,
                        "step": fixture_index,
                    }
                )
                scheduler.acquire_request(cost)
                with _observe_rate_limit(self._runtime, scheduler):
                    result = self._runtime._invoke_hosted(
                        target.runtime_agent_name,
                        session_id,
                        fixture,
                        0,
                        validation_intent_reference=response_intent,
                    )
                    (
                        response_ids,
                        usable,
                        _,
                        _,
                        _,
                        _,
                        _,
                        _,
                    ) = result
                response_references.extend(response_ids)
                usable_results.append(usable)
        else:
            raise ContractError("Validation target runtime kind is not reviewed")
        completed = self._now().astimezone(UTC)
        self._record_duration(
            "endpoint_model_seconds",
            time.monotonic() - endpoint_started,
        )
        return {
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "response_ids": response_references,
            "usable_results": usable_results,
            "session_id": session_id,
        }

    def verify(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        invocation: Mapping[str, Any],
        scheduler: ValidationScheduler,
    ) -> dict[str, Any]:
        if (
            not executing_authority_id
            or conversation_role not in {"baseline", "issue", "paired_v0"}
        ):
            raise ContractError("Validation attempt execution identity is invalid")
        raw_steps = [
            *[("setup", item) for item in attempt["setup_steps"]],
            *[("probe", item) for item in attempt["probe_steps"]],
        ]
        response_references = invocation.get("response_ids")
        usable_results = invocation.get("usable_results")
        session_id = invocation.get("session_id")
        started_at = invocation.get("started_at")
        completed_at = invocation.get("completed_at")
        if (
            not isinstance(response_references, list)
            or not all(isinstance(item, str) and item for item in response_references)
            or not isinstance(usable_results, list)
            or not all(isinstance(item, bool) for item in usable_results)
            or len(response_references) != len(raw_steps)
            or len(usable_results) != len(raw_steps)
            or not isinstance(started_at, str)
            or not isinstance(completed_at, str)
            or (session_id is not None and not isinstance(session_id, str))
        ):
            raise ContractError("Persisted validation invocation is invalid")
        execution_scope = {
            "executing_authority_id": executing_authority_id,
            "target_authority_id": target.authority_id,
            "conversation_role": conversation_role,
            "scenario_id": scenario["id"],
            "conversation_group": attempt["conversation_group"],
            "attempt": attempt["index"],
        }
        invocation_evidence = InvocationEvidence(
            operation_ids=(),
            response_references=tuple(response_references),
            started_at=started_at,
            completed_at=completed_at,
            request_count=len(raw_steps),
            allow_window_correlation=False,
            response_count=len(response_references),
            usable_response_count=sum(usable_results),
            semantic_assertion_count=0,
            semantic_assertions_passed=0,
        )
        telemetry_started = time.monotonic()
        output_messages_states: tuple[tuple[bool, bool], ...] | None = None
        response_anchor_span_ids: tuple[str, ...] | None = None

        def capture_output_messages_states(
            states: tuple[tuple[bool, bool], ...],
        ) -> None:
            nonlocal output_messages_states
            output_messages_states = states

        def capture_response_anchors(anchors: tuple[str, ...]) -> None:
            nonlocal response_anchor_span_ids
            response_anchor_span_ids = anchors

        try:
            with scheduler.telemetry_query():
                operation_ids = self._runtime.wait_for_telemetry(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    invocation=invocation_evidence,
                    allow_shared_operations=True,
                )
            with scheduler.telemetry_query():
                trace_results = self._runtime.trace_assertion_evidence_for_requests(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    operation_ids=operation_ids,
                    response_references=tuple(response_references),
                    window_start=started_at,
                    window_end=completed_at,
                    requests=[
                        {
                            "id": step["id"],
                            "request": step["request"],
                            "expected": {
                                "http_status": step["expected"].get(
                                    "http_status",
                                    200,
                                ),
                                "semantic_assertions": {},
                                "trace_assertions": [],
                            },
                        }
                        for _, step in raw_steps
                    ],
                    stabilization_seconds=self._stabilization_seconds,
                    on_first_pass=lambda: None,
                    on_stable_output_messages=capture_output_messages_states,
                    on_stable_response_anchors=capture_response_anchors,
                    allow_shared_operations=True,
                )
            with scheduler.telemetry_query():
                identity_results = self._runtime.telemetry_identity_passes(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    operation_ids=operation_ids,
                    invocation=invocation_evidence,
                )
        except SharedRuntimeError:
            raise
        except _POST_RESPONSE_TELEMETRY_ERRORS as error:
            raise PostResponseTelemetryError(error) from error
        self._record_duration(
            "ingestion_kql_seconds",
            time.monotonic() - telemetry_started,
        )
        if len(trace_results) != len(raw_steps):
            raise ContractError("Validation trace evidence step count is invalid")
        if len(identity_results) != len(raw_steps):
            raise ContractError("Validation telemetry identity count is invalid")
        if output_messages_states is None:
            raise ContractError("Validation output-message structure state is missing")
        if len(output_messages_states) != len(raw_steps):
            raise ContractError(
                "Validation output-message structure count is invalid"
            )
        if (
            response_anchor_span_ids is None
            or len(response_anchor_span_ids) != len(raw_steps)
            or len(set(response_anchor_span_ids)) != len(raw_steps)
        ):
            raise ContractError("Validation response-anchor mapping is incomplete")

        step_evidence: list[dict[str, Any]] = []
        for index, (
            (_, step),
            response_id,
            operation_id,
            response_anchor_span_id,
            usable,
            identity_pass,
            output_messages_state,
        ) in enumerate(
            zip(
                raw_steps,
                response_references,
                operation_ids,
                response_anchor_span_ids,
                usable_results,
                identity_results,
                output_messages_states,
                strict=True,
            ),
            start=1,
        ):
            step_evidence.append(
                {
                    "index": index,
                    "step_id": step["id"],
                    "request_digest": content_hash(step["request"]),
                    "response_reference": content_hash(
                        {"response_reference": response_id}
                    ),
                    "operation_reference": content_hash(
                        {
                            "operation_reference": operation_id,
                            "response_reference": response_id,
                            "invoke_agent_anchor_span_id": (
                                response_anchor_span_id
                            ),
                        }
                    ),
                    "complete": (
                        bool(usable)
                        and _canonical_output_messages_expectation_passes(
                            output_messages_state,
                            expect_present=True,
                        )
                    ),
                    "endpoint_pass": bool(usable),
                    "identity_pass": identity_pass,
                }
            )
        setup_count = len(attempt["setup_steps"])
        setup_steps = step_evidence[:setup_count]
        probe_steps = step_evidence[setup_count:]
        complete = all(
            item["complete"] and item["endpoint_pass"] and item["identity_pass"]
            for item in step_evidence
        )
        error_code = (
            None
            if complete
            else "telemetry_identity_mismatch"
            if not all(item["identity_pass"] for item in step_evidence)
            else "missing_output_messages_attribute"
            if any(not present for present, _ in output_messages_states)
            else "empty_output_messages_attribute"
            if any(not nonempty for _, nonempty in output_messages_states)
            else "incomplete_endpoint_evidence"
        )
        result = {
            "index": attempt["index"],
            "conversation_reference": content_hash(
                {
                    **execution_scope,
                    "runtime_agent": target.runtime_agent_name,
                }
            ),
            "session_reference": content_hash(
                {**execution_scope, "session_id": session_id}
            ),
            "response_references": [
                item["response_reference"] for item in step_evidence
            ],
            "operation_references": [
                item["operation_reference"] for item in step_evidence
            ],
            "setup_steps": setup_steps,
            "probe_steps": probe_steps,
            "complete": complete,
            "error_code": error_code,
        }
        return result


@contextmanager
def _observe_rate_limit(
    runtime: LiveRuntime,
    scheduler: ValidationScheduler,
) -> Iterator[None]:
    try:
        yield
    finally:
        scheduler.observe_rate_limit(runtime.rate_limit_feedback())
