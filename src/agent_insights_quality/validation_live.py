from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from agent_insights_quality.live import LiveRuntime, _normalize_fixture
from agent_insights_quality.models import InvocationEvidence
from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_evidence import evaluate_defect_predicate
from agent_insights_quality.validation_quota import (
    EndpointCost,
    ValidationScheduler,
)
from agent_insights_quality.validation_runtime import DeployedRuntime


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

    def run(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        expect_defect: bool,
        scheduler: ValidationScheduler,
    ) -> dict[str, Any]:
        with scheduler.runtime_attempt(target.authority_id):
            return self._run(
                target=target,
                executing_authority_id=executing_authority_id,
                conversation_role=conversation_role,
                scenario=scenario,
                attempt=attempt,
                expect_defect=expect_defect,
                scheduler=scheduler,
            )

    def _run(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        expect_defect: bool,
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
                    "expected": step["expected"],
                }
            )
            for _, step in raw_steps
        ]
        started = self._now().astimezone(UTC)
        endpoint_started = time.monotonic()
        response_references: list[str] = []
        semantic_results: list[tuple[int, int]] = []
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
                            }
                        )
                        raise
                    (
                        response_ids,
                        usable,
                        assertion_count,
                        assertions_passed,
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
                semantic_results.append((assertion_count, assertions_passed))
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
                }
            )
            try:
                session_id = self._runtime._create_hosted_session(
                    target.runtime_agent_name,
                    target.runtime_agent_version,
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
                response_name = (
                    f"{target.runtime_agent_name}-{conversation_role}-"
                    f"{attempt['index']}-{fixture_index}"
                )
                self._record_resource(
                    {
                        "state": "create_intent",
                        "kind": "stored_response",
                        "intent_reference": response_intent,
                        "deterministic_name": response_name,
                        "authority_id": target.authority_id,
                        "parent_id": session_id,
                    }
                )
                scheduler.acquire_request(cost)
                with _observe_rate_limit(self._runtime, scheduler):
                    try:
                        result = self._runtime._invoke_hosted(
                            target.runtime_agent_name,
                            session_id,
                            fixture,
                            0,
                        )
                    except ContractError:
                        self._record_resource(
                            {
                                "state": "ambiguous_create",
                                "kind": "stored_response",
                                "intent_reference": response_intent,
                                "deterministic_name": response_name,
                                "authority_id": target.authority_id,
                                "parent_id": session_id,
                            }
                        )
                        raise
                    (
                        response_ids,
                        usable,
                        assertion_count,
                        assertions_passed,
                        _,
                        _,
                        _,
                        _,
                    ) = result
                response_references.extend(response_ids)
                semantic_results.append((assertion_count, assertions_passed))
                usable_results.append(usable)
                for response_id in response_ids:
                    self._record_resource(
                        {
                            "state": "created",
                            "kind": "stored_response",
                            "intent_reference": response_intent,
                            "provider_id": response_id,
                            "deterministic_name": response_id,
                            "authority_id": target.authority_id,
                            "parent_id": session_id,
                        }
                    )
        else:
            raise ContractError("Validation target runtime kind is not reviewed")
        completed = self._now().astimezone(UTC)
        self._record_duration(
            "endpoint_model_seconds",
            time.monotonic() - endpoint_started,
        )
        invocation = InvocationEvidence(
            operation_ids=(),
            response_references=tuple(response_references),
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            request_count=len(fixtures),
            allow_window_correlation=False,
            response_count=len(response_references),
            usable_response_count=sum(usable_results),
            semantic_assertion_count=sum(item[0] for item in semantic_results),
            semantic_assertions_passed=sum(item[1] for item in semantic_results),
        )
        telemetry_started = time.monotonic()
        with scheduler.telemetry_query():
            operation_ids = self._runtime.wait_for_telemetry(
                agent_name=target.runtime_agent_name,
                foundry_version=target.runtime_agent_version,
                invocation=invocation,
            )
        with scheduler.telemetry_query():
            identity_results = self._runtime.telemetry_identity_passes(
                agent_name=target.runtime_agent_name,
                foundry_version=target.runtime_agent_version,
                operation_ids=operation_ids,
                invocation=invocation,
            )
        with scheduler.telemetry_query():
            trace_results = self._runtime.trace_assertion_evidence_for_requests(
                agent_name=target.runtime_agent_name,
                foundry_version=target.runtime_agent_version,
                operation_ids=operation_ids,
                response_references=tuple(response_references),
                window_start=started.isoformat(),
                window_end=completed.isoformat(),
                requests=[
                    {
                        "id": step["id"],
                        "request": step["request"],
                        "expected": step["expected"],
                    }
                    for _, step in raw_steps
                ],
                stabilization_seconds=self._stabilization_seconds,
                on_first_pass=lambda: None,
            )
        self._record_duration(
            "ingestion_kql_seconds",
            time.monotonic() - telemetry_started,
        )
        if len(trace_results) != len(raw_steps):
            raise ContractError("Validation trace evidence step count is invalid")
        if len(identity_results) != len(raw_steps):
            raise ContractError("Validation telemetry identity count is invalid")

        step_evidence: list[dict[str, Any]] = []
        for index, (
            (_, step),
            response_id,
            operation_id,
            semantic,
            trace,
            usable,
            identity_pass,
        ) in enumerate(
            zip(
                raw_steps,
                response_references,
                operation_ids,
                semantic_results,
                trace_results,
                usable_results,
                identity_results,
                strict=True,
            ),
            start=1,
        ):
            semantic_pass = semantic[0] == semantic[1]
            trace_pass = all(item.passed for item in trace)
            step_evidence.append(
                {
                    "index": index,
                    "step_id": step["id"],
                    "request_digest": content_hash(step["request"]),
                    "response_reference": content_hash(
                        {"response_reference": response_id}
                    ),
                    "operation_reference": content_hash(
                        {"operation_reference": operation_id}
                    ),
                    "complete": bool(usable),
                    "endpoint_pass": bool(usable),
                    "semantic_pass": semantic_pass,
                    "trace_pass": trace_pass,
                    "identity_pass": identity_pass,
                }
            )
        setup_count = len(attempt["setup_steps"])
        setup_steps = step_evidence[:setup_count]
        probe_steps = step_evidence[setup_count:]
        complete = all(
            item["complete"] and item["endpoint_pass"] and item["identity_pass"]
            for item in step_evidence
        ) and all(
            item["semantic_pass"] and item["trace_pass"] for item in setup_steps
        )
        observed = evaluate_defect_predicate(
            scenario["defect_predicate"],
            probe_steps,
        )
        healthy = all(
            item["semantic_pass"] and item["trace_pass"] for item in probe_steps
        )
        defect_observed = observed if complete else None
        expected_pass = (
            healthy
            if scenario["validation_mode"] == "baseline"
            else defect_observed is expect_defect
        )
        error_code = (
            None
            if complete
            else "telemetry_identity_mismatch"
            if not all(item["identity_pass"] for item in step_evidence)
            else "incomplete_endpoint_evidence"
        )
        return {
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
            "defect_observed": defect_observed,
            "expected_observation_pass": expected_pass,
            "error_code": error_code,
        }


@contextmanager
def _observe_rate_limit(
    runtime: LiveRuntime,
    scheduler: ValidationScheduler,
) -> Iterator[None]:
    try:
        yield
    finally:
        scheduler.observe_rate_limit(runtime.rate_limit_feedback())
