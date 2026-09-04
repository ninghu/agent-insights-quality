from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

from agent_insights_quality.live import (
    LiveRuntime,
    RemoteOperationError,
    TRACE_ASSERTION_DEADLINE_SECONDS,
    TRACE_ASSERTION_POLL_SECONDS,
    TelemetryOnlyRuntime,
    _canonical_output_messages_expectation_passes,
    _normalize_fixture,
    _semantic_assertion_names,
    _TELEMETRY_TRANSIENT_ERRORS,
)
from agent_insights_quality.models import InvocationEvidence
from agent_insights_quality.util import ContractError, SharedRuntimeError, content_hash
from agent_insights_quality.validation_evidence import attempt_observation
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

    def __init__(self, error: BaseException, *, stage: str) -> None:
        super().__init__("Post-response telemetry verification failed")
        self.code = str(getattr(error, "code", "") or type(error).__name__)
        self.stage = stage
        for field in (
            "matched_reference_count",
            "expected_reference_count",
            "missing_reference_count",
            "invocation_receipt_digest",
            "evidence_window_end",
            "maturity_boundary",
            "snapshot_observed_at",
            "maximum_hydration_seconds",
            "stabilization_seconds",
        ):
            if hasattr(error, field):
                setattr(self, field, getattr(error, field))


class FoundryScenarioAttemptRunner:
    def __init__(
        self,
        runtime: LiveRuntime | TelemetryOnlyRuntime,
        *,
        endpoint_costs: Mapping[str, EndpointCost],
        stabilization_seconds: int,
        record_resource: Callable[[dict[str, Any]], None],
        record_duration: Callable[[str, float], None] = lambda _stage, _value: None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_seconds: int = TRACE_ASSERTION_POLL_SECONDS,
        maximum_wait_seconds: int = TRACE_ASSERTION_DEADLINE_SECONDS,
    ) -> None:
        if (
            stabilization_seconds <= 0
            or poll_seconds <= 0
            or maximum_wait_seconds < stabilization_seconds
        ):
            raise ContractError("Validation telemetry timing policy is invalid")
        self._runtime = runtime
        self._endpoint_costs = dict(endpoint_costs)
        self._stabilization_seconds = stabilization_seconds
        self._record_resource = record_resource
        self._record_duration = record_duration
        self._now = now
        self._poll_seconds = poll_seconds
        self._maximum_wait_seconds = maximum_wait_seconds

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
                self._record_resource(
                    {
                        "state": "create_intent",
                        "kind": "stored_response",
                        "intent_reference": response_intent,
                        "deterministic_name": (
                            f"{target.runtime_agent_name}-"
                            f"{conversation_role}-"
                            f"{attempt['index']}-{fixture_index}"
                        ),
                        "authority_id": target.authority_id,
                        "parent_id": target.provider_agent_id,
                        "runtime_kind": target.runtime_kind,
                        "discovery_key": (
                            f"{target.runtime_agent_name}|{response_intent}"
                        ),
                    }
                )
                scheduler.acquire_request(cost)
                try:
                    with _observe_rate_limit(self._runtime, scheduler):
                        result = self._runtime._invoke_hosted(
                            target.runtime_agent_name,
                            session_id,
                            fixture,
                            0,
                            validation_intent_reference=response_intent,
                        )
                except RemoteOperationError as error:
                    if error.request_accepted is not False:
                        self._record_resource(
                            {
                                "state": "ambiguous_create",
                                "kind": "stored_response",
                                "intent_reference": response_intent,
                                "deterministic_name": (
                                    f"{target.runtime_agent_name}-"
                                    f"{conversation_role}-"
                                    f"{attempt['index']}-{fixture_index}"
                                ),
                                "authority_id": target.authority_id,
                                "parent_id": target.provider_agent_id,
                            }
                        )
                    raise
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
                for response_id in response_ids:
                    self._record_resource(
                        {
                            "state": "created",
                            "kind": "stored_response",
                            "intent_reference": response_intent,
                            "provider_id": response_id,
                            "deterministic_name": response_id,
                            "authority_id": target.authority_id,
                            "parent_id": target.provider_agent_id,
                        }
                    )
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
        return self.verify_attempts(
            target=target,
            executing_authority_id=executing_authority_id,
            conversation_role=conversation_role,
            scenario=scenario,
            attempts=[attempt],
            invocations=[invocation],
            scheduler=scheduler,
        )[0]

    def verify_attempts(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempts: list[Mapping[str, Any]],
        invocations: list[Mapping[str, Any]],
        scheduler: ValidationScheduler,
    ) -> list[dict[str, Any]]:
        if (
            not executing_authority_id
            or conversation_role not in {"baseline", "issue", "paired_v0"}
            or not attempts
            or len(attempts) != len(invocations)
        ):
            raise ContractError("Validation attempt execution identity is invalid")
        batches: list[dict[str, Any]] = []
        all_steps: list[tuple[str, Mapping[str, Any]]] = []
        all_response_references: list[str] = []
        all_usable_results: list[bool] = []
        starts: list[datetime] = []
        completions: list[datetime] = []
        for attempt, invocation in zip(attempts, invocations, strict=True):
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
                or not all(
                    isinstance(item, str) and item
                    for item in response_references
                )
                or not isinstance(usable_results, list)
                or not all(isinstance(item, bool) for item in usable_results)
                or len(response_references) != len(raw_steps)
                or len(usable_results) != len(raw_steps)
                or not isinstance(started_at, str)
                or not isinstance(completed_at, str)
                or (session_id is not None and not isinstance(session_id, str))
            ):
                raise ContractError("Persisted validation invocation is invalid")
            try:
                start = datetime.fromisoformat(started_at).astimezone(UTC)
                completion = datetime.fromisoformat(completed_at).astimezone(UTC)
            except ValueError as error:
                raise ContractError(
                    "Persisted validation invocation window is invalid"
                ) from error
            if completion < start:
                raise ContractError(
                    "Persisted validation invocation window is invalid"
                )
            offset = len(all_steps)
            all_steps.extend(raw_steps)
            all_response_references.extend(response_references)
            all_usable_results.extend(usable_results)
            starts.append(start)
            completions.append(completion)
            batches.append(
                {
                    "attempt": attempt,
                    "session_id": session_id,
                    "offset": offset,
                    "count": len(raw_steps),
                    "setup_count": len(attempt["setup_steps"]),
                }
            )
        invocation_evidence = InvocationEvidence(
            operation_ids=(),
            response_references=tuple(all_response_references),
            started_at=min(starts).isoformat(),
            completed_at=max(completions).isoformat(),
            request_count=len(all_steps),
            allow_window_correlation=False,
            response_count=len(all_response_references),
            usable_response_count=sum(all_usable_results),
            semantic_assertion_count=0,
            semantic_assertions_passed=0,
        )
        telemetry_started = time.monotonic()
        output_messages_states: tuple[tuple[bool, bool], ...] | None = None
        response_anchor_span_ids: tuple[str, ...] | None = None
        semantic_results: tuple[tuple[Any, ...], ...] | None = None

        def capture_output_messages_states(
            states: tuple[tuple[bool, bool], ...],
        ) -> None:
            nonlocal output_messages_states
            output_messages_states = states

        def capture_response_anchors(anchors: tuple[str, ...]) -> None:
            nonlocal response_anchor_span_ids
            response_anchor_span_ids = anchors

        def capture_semantic_results(results: tuple[tuple[Any, ...], ...]) -> None:
            nonlocal semantic_results
            semantic_results = results

        query_stage = "telemetry_discovery"
        try:
            with scheduler.telemetry_query():
                operation_ids = self._runtime.wait_for_telemetry(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    invocation=invocation_evidence,
                    allow_shared_operations=True,
                    poll_seconds=self._poll_seconds,
                    maximum_wait_seconds=self._maximum_wait_seconds,
                )
            query_stage = "trace_output_stability"
            with scheduler.telemetry_query():
                trace_results = self._runtime.trace_assertion_evidence_for_requests(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    operation_ids=operation_ids,
                    response_references=tuple(all_response_references),
                    window_start=invocation_evidence.started_at,
                    window_end=invocation_evidence.completed_at,
                    requests=[
                        {
                            "id": step["id"],
                            "request": step["request"],
                            "expected": step["expected"],
                        }
                        for _, step in all_steps
                    ],
                    stabilization_seconds=self._stabilization_seconds,
                    poll_seconds=self._poll_seconds,
                    maximum_wait_seconds=self._maximum_wait_seconds,
                    on_first_pass=lambda: None,
                    on_stable_output_messages=capture_output_messages_states,
                    on_stable_response_anchors=capture_response_anchors,
                    on_stable_semantic_assertions=capture_semantic_results,
                    allow_shared_operations=True,
                )
            query_stage = "telemetry_identity"
            with scheduler.telemetry_query():
                identity_results = self._runtime.telemetry_identity_passes(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    operation_ids=operation_ids,
                    invocation=invocation_evidence,
                    poll_seconds=self._poll_seconds,
                    maximum_wait_seconds=self._maximum_wait_seconds,
                )
        except SharedRuntimeError:
            raise
        except _POST_RESPONSE_TELEMETRY_ERRORS as error:
            raise PostResponseTelemetryError(error, stage=query_stage) from error
        self._record_duration(
            "ingestion_kql_seconds",
            time.monotonic() - telemetry_started,
        )
        if len(trace_results) != len(all_steps):
            raise ContractError("Validation trace evidence step count is invalid")
        if len(identity_results) != len(all_steps):
            raise ContractError("Validation telemetry identity count is invalid")
        if output_messages_states is None:
            raise ContractError("Validation output-message structure state is missing")
        if len(output_messages_states) != len(all_steps):
            raise ContractError(
                "Validation output-message structure count is invalid"
            )
        if (
            response_anchor_span_ids is None
            or len(response_anchor_span_ids) != len(all_steps)
            or len(set(response_anchor_span_ids)) != len(all_steps)
        ):
            raise ContractError("Validation response-anchor mapping is incomplete")
        if semantic_results is None or len(semantic_results) != len(all_steps):
            raise ContractError("Validation semantic assertion state is missing")

        results = []
        for batch in batches:
            attempt = batch["attempt"]
            offset = batch["offset"]
            end = offset + batch["count"]
            step_evidence: list[dict[str, Any]] = []
            for index, (
                (_, step),
                response_id,
                operation_id,
                response_anchor_span_id,
                usable,
                identity_pass,
                output_messages_state,
                step_trace_results,
                step_semantic_results,
            ) in enumerate(
                zip(
                    all_steps[offset:end],
                    all_response_references[offset:end],
                    operation_ids[offset:end],
                    response_anchor_span_ids[offset:end],
                    all_usable_results[offset:end],
                    identity_results[offset:end],
                    output_messages_states[offset:end],
                    trace_results[offset:end],
                    semantic_results[offset:end],
                    strict=True,
                ),
                start=1,
            ):
                output_complete = _canonical_output_messages_expectation_passes(
                    output_messages_state,
                    expect_present=True,
                )
                semantic_complete = len(step_semantic_results) == len(
                    _semantic_assertion_names(
                        step["expected"]["semantic_assertions"]
                    )
                )
                trace_complete = len(step_trace_results) == len(
                    step["expected"]["trace_assertions"]
                )
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
                        "complete": bool(usable)
                        and bool(identity_pass)
                        and output_complete
                        and semantic_complete
                        and trace_complete,
                        "endpoint_pass": bool(usable),
                        "identity_pass": bool(identity_pass),
                        "semantic_pass": semantic_complete
                        and all(item.passed for item in step_semantic_results),
                        "trace_pass": trace_complete
                        and all(item.passed for item in step_trace_results),
                    }
                )
            setup_count = batch["setup_count"]
            setup_steps = step_evidence[:setup_count]
            probe_steps = step_evidence[setup_count:]
            complete = all(item["complete"] for item in step_evidence)
            error_code = (
                None
                if complete
                else "telemetry_identity_mismatch"
                if not all(item["identity_pass"] for item in step_evidence)
                else "missing_output_messages_attribute"
                if any(
                    not present
                    for present, _ in output_messages_states[offset:end]
                )
                else "empty_output_messages_attribute"
                if any(
                    not nonempty
                    for _, nonempty in output_messages_states[offset:end]
                )
                else "incomplete_assertion_evidence"
            )
            execution_scope = {
                "executing_authority_id": executing_authority_id,
                "target_authority_id": target.authority_id,
                "conversation_role": conversation_role,
                "scenario_id": scenario["id"],
                "conversation_group": attempt["conversation_group"],
                "attempt": attempt["index"],
            }
            results.append(
                {
                    "index": attempt["index"],
                    "conversation_reference": content_hash(
                        {
                            **execution_scope,
                            "runtime_agent": target.runtime_agent_name,
                        }
                    ),
                    "session_reference": content_hash(
                        {
                            **execution_scope,
                            "session_id": batch["session_id"],
                        }
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
                    "observation": attempt_observation(
                        scenario,
                        probe_steps,
                    ),
                    "error_code": error_code,
                }
            )
        return results


class FoundryScenarioVerifier:
    def __init__(
        self,
        runtime: TelemetryOnlyRuntime,
        *,
        endpoint_costs: Mapping[str, EndpointCost],
        stabilization_seconds: int,
        record_duration: Callable[[str, float], None] = lambda _stage, _value: None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_seconds: int = TRACE_ASSERTION_POLL_SECONDS,
        maximum_wait_seconds: int = TRACE_ASSERTION_DEADLINE_SECONDS,
    ) -> None:
        self._runtime = runtime
        self._stabilization_seconds = stabilization_seconds
        self._record_duration = record_duration
        self._now = now
        self._poll_seconds = poll_seconds
        self._maximum_wait_seconds = maximum_wait_seconds
        self.__delegate = FoundryScenarioAttemptRunner(
            runtime,
            endpoint_costs=endpoint_costs,
            stabilization_seconds=stabilization_seconds,
            record_resource=lambda _event: None,
            record_duration=record_duration,
            now=now,
            poll_seconds=poll_seconds,
            maximum_wait_seconds=maximum_wait_seconds,
        )

    def verify(self, **kwargs: Any) -> dict[str, Any]:
        return self.__delegate.verify(**kwargs)

    def verify_attempts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.__delegate.verify_attempts(**kwargs)

    def collect_attempts(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempts: list[Mapping[str, Any]],
        invocations: list[Mapping[str, Any]],
        scheduler: ValidationScheduler,
        invocation_receipt_digest: str,
    ) -> dict[str, Any]:
        if (
            not executing_authority_id
            or conversation_role not in {"baseline", "issue", "paired_v0"}
            or not attempts
            or len(attempts) != len(invocations)
        ):
            raise ContractError("Validation attempt execution identity is invalid")
        batches: list[dict[str, Any]] = []
        all_steps: list[tuple[str, Mapping[str, Any]]] = []
        response_ids: list[str] = []
        usable_results: list[bool] = []
        starts: list[datetime] = []
        completions: list[datetime] = []
        for attempt, invocation in zip(attempts, invocations, strict=True):
            steps = [
                *[("setup", item) for item in attempt["setup_steps"]],
                *[("probe", item) for item in attempt["probe_steps"]],
            ]
            attempt_responses = invocation.get("response_ids")
            attempt_usable = invocation.get("usable_results")
            session_id = invocation.get("session_id")
            started_at = invocation.get("started_at")
            completed_at = invocation.get("completed_at")
            if (
                not isinstance(attempt_responses, list)
                or not all(
                    isinstance(item, str) and item
                    for item in attempt_responses
                )
                or not isinstance(attempt_usable, list)
                or not all(isinstance(item, bool) for item in attempt_usable)
                or len(attempt_responses) != len(steps)
                or len(attempt_usable) != len(steps)
                or not isinstance(started_at, str)
                or not isinstance(completed_at, str)
                or (session_id is not None and not isinstance(session_id, str))
            ):
                raise ContractError("Persisted validation invocation is invalid")
            try:
                start = datetime.fromisoformat(started_at).astimezone(UTC)
                completion = datetime.fromisoformat(completed_at).astimezone(UTC)
            except ValueError as error:
                raise ContractError(
                    "Persisted validation invocation window is invalid"
                ) from error
            if completion < start:
                raise ContractError(
                    "Persisted validation invocation window is invalid"
                )
            offset = len(all_steps)
            all_steps.extend(steps)
            response_ids.extend(attempt_responses)
            usable_results.extend(attempt_usable)
            starts.append(start)
            completions.append(completion)
            batches.append(
                {
                    "attempt": attempt,
                    "session_id": session_id,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "response_ids": list(attempt_responses),
                    "usable_results": list(attempt_usable),
                    "offset": offset,
                    "count": len(steps),
                }
            )
        invocation_evidence = InvocationEvidence(
            operation_ids=(),
            response_references=tuple(response_ids),
            started_at=min(starts).isoformat(),
            completed_at=max(completions).isoformat(),
            request_count=len(all_steps),
            allow_window_correlation=False,
            response_count=len(response_ids),
            usable_response_count=sum(usable_results),
            semantic_assertion_count=0,
            semantic_assertions_passed=0,
        )
        evidence_window_start = min(starts)
        evidence_window_end = max(completions)
        maturity_proof = evidence_maturity_proof(
            invocation_receipt_digest=invocation_receipt_digest,
            evidence_window_start=evidence_window_start,
            evidence_window_end=evidence_window_end,
            snapshot_observed_at=self._now().astimezone(UTC),
            maximum_hydration_seconds=self._maximum_wait_seconds,
            stabilization_seconds=self._stabilization_seconds,
        )
        telemetry_started = time.monotonic()
        query_stage = "telemetry_discovery"
        trace_hydration_state: str | None = None

        def capture_trace_hydration_state(state: str) -> None:
            nonlocal trace_hydration_state
            trace_hydration_state = state

        predicate = scenario["defect_predicate"]
        required_trace_step_ids = (
            {
                step["id"]
                for attempt in attempts
                for step in attempt["probe_steps"]
            }
            if predicate["kind"] == "never"
            else set(predicate["step_ids"])
            if "trace" in predicate["required_surfaces"]
            else set()
        )
        trace_attempt_indexes = tuple(
            tuple(
                index
                for index in range(
                    int(batch["offset"]),
                    int(batch["offset"]) + int(batch["count"]),
                )
                if (
                    all_steps[index][0] == "setup"
                    or all_steps[index][1]["id"] in required_trace_step_ids
                )
                and all_steps[index][1]["expected"]["trace_assertions"]
            )
            for batch in batches
        )
        trace_requests = [
            {
                "id": step["id"],
                "request": step["request"],
                "expected": step["expected"],
            }
            for _, step in all_steps
        ]
        try:
            with scheduler.telemetry_query():
                operation_ids = self._runtime.wait_for_telemetry(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    invocation=invocation_evidence,
                    allow_shared_operations=True,
                    poll_seconds=self._poll_seconds,
                    maximum_wait_seconds=self._maximum_wait_seconds,
                    mature=maturity_proof["mature"],
                    maturity_proof=maturity_proof,
                )
            query_stage = "trace_output_stability"
            with scheduler.telemetry_query():
                trace_rows, anchor_span_ids = (
                    self._runtime.stable_correlated_evidence_for_requests(
                        agent_name=target.runtime_agent_name,
                        foundry_version=target.runtime_agent_version,
                        operation_ids=operation_ids,
                        response_references=tuple(response_ids),
                        window_start=invocation_evidence.started_at,
                        window_end=invocation_evidence.completed_at,
                        stabilization_seconds=self._stabilization_seconds,
                        poll_seconds=self._poll_seconds,
                        maximum_wait_seconds=self._maximum_wait_seconds,
                        requests=trace_requests,
                        required_trace_attempt_indexes=trace_attempt_indexes,
                        minimum_complete_trace_attempts=len(attempts),
                        mature=maturity_proof["mature"],
                        maturity_proof=maturity_proof,
                        on_hydration_state=capture_trace_hydration_state,
                    )
                )
            query_stage = "telemetry_identity"
            with scheduler.telemetry_query():
                identity_results = self._runtime.telemetry_identity_passes(
                    agent_name=target.runtime_agent_name,
                    foundry_version=target.runtime_agent_version,
                    operation_ids=operation_ids,
                    invocation=invocation_evidence,
                    poll_seconds=self._poll_seconds,
                    maximum_wait_seconds=self._maximum_wait_seconds,
                    mature=maturity_proof["mature"],
                )
        except SharedRuntimeError:
            raise
        except _POST_RESPONSE_TELEMETRY_ERRORS as error:
            raise PostResponseTelemetryError(error, stage=query_stage) from error
        self._record_duration(
            "ingestion_kql_seconds",
            time.monotonic() - telemetry_started,
        )
        if trace_hydration_state is None:
            raise ContractError("Validation trace hydration state is missing")
        maturity_proof = evidence_maturity_proof(
            invocation_receipt_digest=invocation_receipt_digest,
            evidence_window_start=evidence_window_start,
            evidence_window_end=evidence_window_end,
            snapshot_observed_at=self._now().astimezone(UTC),
            maximum_hydration_seconds=self._maximum_wait_seconds,
            stabilization_seconds=self._stabilization_seconds,
        )
        maturity_proof["required_trace_hydration"] = trace_hydration_state
        maturity_proof["maturity_proof_digest"] = content_hash(
            {
                key: value
                for key, value in maturity_proof.items()
                if key != "maturity_proof_digest"
            }
        )
        if not (
            len(operation_ids)
            == len(trace_rows)
            == len(anchor_span_ids)
            == len(identity_results)
            == len(all_steps)
        ):
            raise ContractError("Validation private evidence coverage is incomplete")
        results = []
        for batch in batches:
            offset = batch["offset"]
            end = offset + batch["count"]
            step_values = []
            for position, (
                (phase, step),
                response_id,
                usable,
                operation_id,
                anchor_span_id,
                identity_pass,
                rows,
            ) in enumerate(
                zip(
                    all_steps[offset:end],
                    response_ids[offset:end],
                    usable_results[offset:end],
                    operation_ids[offset:end],
                    anchor_span_ids[offset:end],
                    identity_results[offset:end],
                    trace_rows[offset:end],
                    strict=True,
                ),
                start=1,
            ):
                step_values.append(
                    {
                        "index": position,
                        "phase": phase,
                        "step_id": step["id"],
                        "request": json.loads(json.dumps(step["request"])),
                        "expected": json.loads(json.dumps(step["expected"])),
                        "response_id": response_id,
                        "usable_response": usable,
                        "operation_id": operation_id,
                        "invoke_agent_anchor_span_id": anchor_span_id,
                        "identity_pass": bool(identity_pass),
                        "trace_rows": json.loads(
                            json.dumps(list(rows), default=str)
                        ),
                    }
                )
            results.append(
                {
                    "index": batch["attempt"]["index"],
                    "conversation_group": batch["attempt"][
                        "conversation_group"
                    ],
                    "parameters": json.loads(
                        json.dumps(batch["attempt"]["parameters"])
                    ),
                    "started_at": batch["started_at"],
                    "completed_at": batch["completed_at"],
                    "session_id": batch["session_id"],
                    "response_ids": batch["response_ids"],
                    "usable_results": batch["usable_results"],
                    "steps": step_values,
                }
            )
        return {
            "attempts": results,
            "evidence_snapshot": maturity_proof,
        }


def evidence_maturity_proof(
    *,
    invocation_receipt_digest: str,
    evidence_window_start: datetime,
    evidence_window_end: datetime,
    snapshot_observed_at: datetime,
    maximum_hydration_seconds: int,
    stabilization_seconds: int,
) -> dict[str, Any]:
    start = evidence_window_start.astimezone(UTC)
    end = evidence_window_end.astimezone(UTC)
    observed = snapshot_observed_at.astimezone(UTC)
    if (
        evidence_window_start.tzinfo is None
        or evidence_window_end.tzinfo is None
        or snapshot_observed_at.tzinfo is None
        or not invocation_receipt_digest.startswith("sha256:")
        or len(invocation_receipt_digest.removeprefix("sha256:")) != 64
        or any(
            character not in "0123456789abcdef"
            for character in invocation_receipt_digest.removeprefix("sha256:")
        )
        or end < start
        or maximum_hydration_seconds < 1
        or stabilization_seconds < 1
    ):
        raise ContractError("Validation evidence maturity inputs are invalid")
    boundary = end + timedelta(
        seconds=maximum_hydration_seconds + stabilization_seconds
    )
    mature = observed >= boundary
    value = {
        "schema_version": "1.0.0",
        "invocation_receipt_digest": invocation_receipt_digest,
        "evidence_window_start": start.isoformat(),
        "evidence_window_end": end.isoformat(),
        "maturity_boundary": boundary.isoformat(),
        "snapshot_observed_at": observed.isoformat(),
        "maximum_hydration_seconds": maximum_hydration_seconds,
        "stabilization_seconds": stabilization_seconds,
        "mature": mature,
        "snapshot_mode": (
            "mature_single_snapshot" if mature else "bounded_hydration"
        ),
        "maturity_proof_digest": "",
    }
    value["maturity_proof_digest"] = content_hash(
        {
            key: item
            for key, item in value.items()
            if key != "maturity_proof_digest"
        }
    )
    return value
@contextmanager
def _observe_rate_limit(
    runtime: LiveRuntime,
    scheduler: ValidationScheduler,
) -> Iterator[None]:
    try:
        yield
    finally:
        scheduler.observe_rate_limit(runtime.rate_limit_feedback())
