from __future__ import annotations

from datetime import UTC, datetime

import pytest

import agent_insights_quality.validation_live as validation_live
from agent_insights_quality.live import TelemetryCorrelationError
from agent_insights_quality.models import TraceAssertionEvidence
from agent_insights_quality.validation_live import FoundryScenarioAttemptRunner
from agent_insights_quality.validation_quota import (
    CapacityPlan,
    EndpointCost,
    ValidationScheduler,
    WeightedTokenBucket,
)
from agent_insights_quality.validation_runtime import DeployedRuntime
from agent_insights_quality.validation_rules import (
    CONVERSATION_PLACEHOLDER,
    RUNTIME_AGENT_NAME_PLACEHOLDER,
    RUNTIME_AGENT_VERSION_PLACEHOLDER,
)
from agent_insights_quality.util import ContractError

HASH = "sha256:" + ("a" * 64)


def _scheduler() -> ValidationScheduler:
    return ValidationScheduler(
        CapacityPlan(
            measured_rpm=100,
            measured_tpm=100000,
            measured_at="2026-08-29T00:00:00Z",
            reserved_percent=25,
            reserved_rpm=25,
            reserved_tpm=25000,
            available_rpm=75,
            available_tpm=75000,
            outer_request_envelope=20,
            worst_case_inner_model_calls=1,
            worst_case_inner_tokens=10,
            endpoint_concurrency=2,
            provisioning_concurrency=8,
            telemetry_query_concurrency=4,
            runtime_attempt_concurrency=1,
            inner_model_call_limit=4,
            plan_digest=HASH,
        ),
        WeightedTokenBucket(request_capacity=100, token_capacity=10000),
    )


def _step(step_id: str, *, probe: bool) -> dict:
    return {
        "id": step_id,
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
                                "text": f"Synthetic {step_id}.",
                            }
                        ],
                    }
                ],
                "conversation": {"id": CONVERSATION_PLACEHOLDER},
            },
        },
        "expected": {
            "http_status": 200,
            "semantic_assertions": (
                {"required_terms_all": ["synthetic"]} if probe else {}
            ),
            "trace_assertions": (
                [{"name": "synthetic_trace", "kind": "tool_call_count", "tool_name": "demo", "count": 1}]
                if probe
                else []
            ),
            "identity_assertions": {
                "agent_name": RUNTIME_AGENT_NAME_PLACEHOLDER,
                "agent_version": RUNTIME_AGENT_VERSION_PLACEHOLDER,
            },
        },
    }


class Runtime:
    def __init__(
        self,
        *,
        assertion_pass: bool = True,
        identity_pass: bool = True,
        output_messages_state: tuple[bool, bool] = (True, True),
    ) -> None:
        self.assertion_pass = assertion_pass
        self.identity_pass = identity_pass
        self.output_messages_state = output_messages_state
        self.counter = 0
        self.telemetry_counter = 0

    def _invoke_prompt(
        self,
        agent_name,
        foundry_version,
        fixture,
        seed,
        previous_response_id,
        *,
        include_seed_metadata,
        validation_intent_reference,
    ):
        del agent_name, foundry_version, seed, previous_response_id
        assert include_seed_metadata is False
        assert validation_intent_reference.startswith("sha256:")
        self.counter += 1
        count = len(fixture["semantic_assertions"])
        passed = count if self.assertion_pass or not count else 0
        return (
            [f"response-{self.counter}"],
            True,
            count,
            passed,
            1,
            0,
            (),
            False,
        )

    def wait_for_telemetry(self, **kwargs):
        values = tuple(
            f"{self.telemetry_counter + index + 1:032x}"
            for index in range(kwargs["invocation"].request_count)
        )
        self.telemetry_counter += kwargs["invocation"].request_count
        return values

    def telemetry_identity_passes(self, **kwargs):
        return tuple(
            self.identity_pass for _ in kwargs["operation_ids"]
        )

    def canonical_output_messages_state(self, operation_ids):
        return tuple(self.output_messages_state for _ in operation_ids)

    @staticmethod
    def rate_limit_feedback():
        return {
            "remaining_requests": None,
            "remaining_tokens": None,
            "retry_after_seconds": None,
        }

    def trace_assertion_evidence_for_requests(self, **kwargs):
        return tuple(
            (
                TraceAssertionEvidence(
                    assertion="synthetic_trace",
                    passed=self.assertion_pass,
                ),
            )
            if request["expected"]["trace_assertions"]
            else ()
            for request in kwargs["requests"]
        )


class HostedRuntime(Runtime):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.deleted_sessions = []
        self.activations = []
        self.session_intents = []

    def _activate_hosted_version(
        self,
        agent_name,
        foundry_version,
        *,
        refresh_route=False,
    ):
        self.activations.append((agent_name, foundry_version, refresh_route))

    def _create_hosted_session(
        self,
        agent_name,
        foundry_version,
        *,
        validation_intent_reference,
    ):
        del agent_name, foundry_version
        assert validation_intent_reference.startswith("sha256:")
        self.session_intents.append(validation_intent_reference)
        return f"session-{len(self.session_intents)}"

    def _invoke_hosted(
        self,
        agent_name,
        session_id,
        fixture,
        seed,
        *,
        validation_intent_reference,
    ):
        del agent_name, session_id, seed
        assert validation_intent_reference.startswith("sha256:")
        self.counter += 1
        count = len(fixture["semantic_assertions"])
        return (
            [f"response-{self.counter}"],
            True,
            count,
            count,
            1,
            0,
            (),
            False,
        )

    def _delete_hosted_session(self, agent_name, session_id):
        self.deleted_sessions.append((agent_name, session_id))


class TelemetryFailureRuntime(Runtime):
    def wait_for_telemetry(self, **kwargs):
        raise TelemetryCorrelationError(
            matched_reference_count=1,
            expected_reference_count=kwargs["invocation"].request_count,
        )


def _target() -> DeployedRuntime:
    return DeployedRuntime(
        authority_id="issue-001",
        runtime_kind="prompt",
        runtime_agent_name="weather-agent-issue-001-cycle",
        runtime_agent_version="1",
        provider_agent_id="provider-agent",
        provider_agent_version_id="provider-version",
        hosted_identity_id=None,
        hosted_blueprint_id=None,
        hosted_deployment_id=None,
        runtime_principal_id=None,
        telemetry_identity_id="provider-version",
        connection_ids=(),
    )


def _hosted_target() -> DeployedRuntime:
    return DeployedRuntime(
        authority_id="issue-013",
        runtime_kind="hosted_code",
        runtime_agent_name="finance-agent-issue-013-cycle",
        runtime_agent_version="1",
        provider_agent_id="provider-agent",
        provider_agent_version_id="provider-version",
        hosted_identity_id="hosted-identity",
        hosted_blueprint_id="hosted-blueprint",
        hosted_deployment_id="hosted-deployment",
        runtime_principal_id="runtime-principal",
        telemetry_identity_id="provider-version",
        connection_ids=(),
    )


def test_attempt_keeps_completion_independent_from_defect_observation() -> None:
    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    resources = []
    runner = FoundryScenarioAttemptRunner(
        Runtime(assertion_pass=False),
        endpoint_costs={"issue-001": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=resources.append,
        now=lambda: next(times),
    )
    setup = _step("setup-1", probe=False)
    probe = _step("probe-1", probe=True)
    scenario = {
        "id": "reviewed-path",
        "validation_mode": "model_mediated",
        "defect_predicate": {
            "kind": "all_observation_steps_pass",
            "step_ids": ["probe-1"],
            "required_surfaces": ["semantic", "trace"],
        },
    }
    result = runner.run(
        target=_target(),
        executing_authority_id="issue-001",
        conversation_role="issue",
        scenario=scenario,
        attempt={
            "index": 1,
            "conversation_group": "attempt-1",
            "setup_steps": [setup],
            "probe_steps": [probe],
        },
        expect_defect=True,
        scheduler=_scheduler(),
    )
    assert result["complete"] is True
    assert result["defect_observed"] is False
    assert result["expected_observation_pass"] is False
    assert len(resources) == 4
    assert all(resource["kind"] == "stored_response" for resource in resources)
    assert all(
        resource["runtime_kind"] == "prompt"
        and resource["discovery_key"]
        for resource in resources
        if resource["state"] == "create_intent"
    )
    assert [resource["state"] for resource in resources] == [
        "create_intent",
        "created",
        "create_intent",
        "created",
    ]


def test_passing_probe_is_observed_and_evidence_contains_hashes_only() -> None:
    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    runner = FoundryScenarioAttemptRunner(
        Runtime(assertion_pass=True),
        endpoint_costs={"issue-001": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=lambda item: None,
        now=lambda: next(times),
    )
    result = runner.run(
        target=_target(),
        executing_authority_id="issue-001",
        conversation_role="issue",
        scenario={
            "id": "reviewed-path",
            "validation_mode": "model_mediated",
            "defect_predicate": {
                "kind": "all_observation_steps_pass",
                "step_ids": ["probe-1"],
                "required_surfaces": ["semantic", "trace"],
            },
        },
        attempt={
            "index": 1,
            "conversation_group": "attempt-1",
            "setup_steps": [_step("setup-1", probe=False)],
            "probe_steps": [_step("probe-1", probe=True)],
        },
        expect_defect=True,
        scheduler=_scheduler(),
    )
    assert result["defect_observed"] is True
    assert result["expected_observation_pass"] is True
    assert all(
        reference.startswith("sha256:")
        for reference in [
            result["conversation_reference"],
            result["session_reference"],
            *result["response_references"],
            *result["operation_references"],
        ]
    )


def test_telemetry_identity_mismatch_keeps_attempt_incomplete() -> None:
    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    runner = FoundryScenarioAttemptRunner(
        Runtime(identity_pass=False),
        endpoint_costs={"issue-001": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=lambda item: None,
        now=lambda: next(times),
    )
    result = runner.run(
        target=_target(),
        executing_authority_id="issue-001",
        conversation_role="issue",
        scenario={
            "id": "reviewed-path",
            "validation_mode": "model_mediated",
            "defect_predicate": {
                "kind": "all_observation_steps_pass",
                "step_ids": ["probe-1"],
                "required_surfaces": ["semantic", "trace"],
            },
        },
        attempt={
            "index": 1,
            "conversation_group": "attempt-1",
            "setup_steps": [_step("setup-1", probe=False)],
            "probe_steps": [_step("probe-1", probe=True)],
        },
        expect_defect=True,
        scheduler=_scheduler(),
    )
    assert result["complete"] is False
    assert result["defect_observed"] is None
    assert result["error_code"] == "telemetry_identity_mismatch"
    assert all(
        step["identity_pass"] is False
        for step in [*result["setup_steps"], *result["probe_steps"]]
    )


@pytest.mark.parametrize(
    ("state", "error_code"),
    [
        ((False, False), "missing_output_messages_attribute"),
        ((True, False), "empty_output_messages_attribute"),
    ],
)
def test_canonical_output_messages_failure_keeps_issue_attempt_incomplete(
    state,
    error_code,
) -> None:
    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    runner = FoundryScenarioAttemptRunner(
        Runtime(output_messages_state=state),
        endpoint_costs={"issue-001": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=lambda item: None,
        now=lambda: next(times),
    )
    result = runner.run(
        target=_target(),
        executing_authority_id="issue-001",
        conversation_role="issue",
        scenario={
            "id": "reviewed-path",
            "validation_mode": "model_mediated",
            "defect_predicate": {
                "kind": "all_observation_steps_pass",
                "step_ids": ["probe-1"],
                "required_surfaces": ["semantic", "trace"],
            },
        },
        attempt={
            "index": 1,
            "conversation_group": "attempt-1",
            "setup_steps": [_step("setup-1", probe=False)],
            "probe_steps": [_step("probe-1", probe=True)],
        },
        expect_defect=True,
        scheduler=_scheduler(),
    )

    assert result["complete"] is False
    assert result["defect_observed"] is None
    assert result["error_code"] == error_code
    assert all(
        step["endpoint_pass"] is True
        for step in [*result["setup_steps"], *result["probe_steps"]]
    )


def test_post_response_telemetry_failure_keeps_request_accepted(
    monkeypatch,
) -> None:
    class SyntheticTelemetryError(Exception):
        pass

    monkeypatch.setattr(
        validation_live,
        "_POST_RESPONSE_TELEMETRY_ERRORS",
        (*validation_live._POST_RESPONSE_TELEMETRY_ERRORS, SyntheticTelemetryError),
    )

    class FailedTelemetryRuntime(Runtime):
        @staticmethod
        def telemetry_identity_passes(**_kwargs):
            raise SyntheticTelemetryError("Synthetic telemetry failure")

    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    runner = FoundryScenarioAttemptRunner(
        FailedTelemetryRuntime(),
        endpoint_costs={"issue-001": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=lambda item: None,
        now=lambda: next(times),
    )

    with pytest.raises(ContractError) as caught:
        runner.run(
            target=_target(),
            executing_authority_id="issue-001",
            conversation_role="issue",
            scenario={
                "id": "reviewed-path",
                "validation_mode": "model_mediated",
                "defect_predicate": {
                    "kind": "all_observation_steps_pass",
                    "step_ids": ["probe-1"],
                    "required_surfaces": ["semantic", "trace"],
                },
            },
            attempt={
                "index": 1,
                "conversation_group": "attempt-1",
                "setup_steps": [_step("setup-1", probe=False)],
                "probe_steps": [_step("probe-1", probe=True)],
            },
            expect_defect=True,
            scheduler=_scheduler(),
        )

    assert caught.value.request_accepted is True


def test_post_response_correlation_failure_preserves_counts_and_acceptance() -> None:
    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    runner = FoundryScenarioAttemptRunner(
        TelemetryFailureRuntime(),
        endpoint_costs={"issue-001": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=lambda item: None,
        now=lambda: next(times),
    )

    with pytest.raises(ContractError) as caught:
        runner.run(
            target=_target(),
            executing_authority_id="issue-001",
            conversation_role="issue",
            scenario={
                "id": "reviewed-path",
                "validation_mode": "model_mediated",
                "defect_predicate": {
                    "kind": "all_observation_steps_pass",
                    "step_ids": ["probe-1"],
                    "required_surfaces": ["semantic", "trace"],
                },
            },
            attempt={
                "index": 1,
                "conversation_group": "attempt-1",
                "setup_steps": [_step("setup-1", probe=False)],
                "probe_steps": [_step("probe-1", probe=True)],
            },
            expect_defect=True,
            scheduler=_scheduler(),
        )

    assert caught.value.request_accepted is True
    assert caught.value.code == "telemetry_correlation_timeout"
    assert caught.value.matched_reference_count == 1
    assert caught.value.expected_reference_count == 2
    assert caught.value.missing_reference_count == 1


def test_shared_v0_attempts_have_unique_execution_and_resource_references() -> None:
    current = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    def now() -> datetime:
        nonlocal current
        value = current
        current = current.replace(second=current.second + 1)
        return value

    resources = []
    runner = FoundryScenarioAttemptRunner(
        Runtime(),
        endpoint_costs={
            "issue-001": EndpointCost(1, 10, 1),
            "issue-002": EndpointCost(1, 10, 1),
        },
        stabilization_seconds=1,
        record_resource=resources.append,
        now=now,
    )
    scenario = {
        "id": "reviewed-path",
        "validation_mode": "model_mediated",
        "defect_predicate": {
            "kind": "all_observation_steps_pass",
            "step_ids": ["probe-1"],
            "required_surfaces": ["semantic", "trace"],
        },
    }
    attempt = {
        "index": 1,
        "conversation_group": "attempt-1",
        "setup_steps": [_step("setup-1", probe=False)],
        "probe_steps": [_step("probe-1", probe=True)],
    }
    first = runner.run(
        target=_target(),
        executing_authority_id="issue-001",
        conversation_role="paired_v0",
        scenario=scenario,
        attempt=attempt,
        expect_defect=False,
        scheduler=_scheduler(),
    )
    second = runner.run(
        target=_target(),
        executing_authority_id="issue-002",
        conversation_role="paired_v0",
        scenario=scenario,
        attempt=attempt,
        expect_defect=False,
        scheduler=_scheduler(),
    )
    assert first["conversation_reference"] != second["conversation_reference"]
    assert first["session_reference"] != second["session_reference"]
    intents = [
        item["intent_reference"]
        for item in resources
        if item["state"] == "create_intent"
    ]
    assert len(intents) == len(set(intents))


def test_repeated_hosted_attempts_refresh_routes_and_release_unique_sessions() -> None:
    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 1, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 1, 1, tzinfo=UTC),
        ]
    )
    resources = []
    runtime = HostedRuntime()
    runner = FoundryScenarioAttemptRunner(
        runtime,
        endpoint_costs={"issue-013": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=resources.append,
        now=lambda: next(times),
    )
    target = _hosted_target()
    scenario = {
        "id": "reviewed-path",
        "validation_mode": "deterministic",
        "defect_predicate": {
            "kind": "all_observation_steps_pass",
            "step_ids": ["probe-1"],
            "required_surfaces": ["semantic", "trace"],
        },
    }
    runner.prepare_hosted_routes([target])
    for attempt_index in (1, 2):
        runner.run(
            target=target,
            executing_authority_id="issue-013",
            conversation_role="issue",
            scenario=scenario,
            attempt={
                "index": attempt_index,
                "conversation_group": f"attempt-{attempt_index}",
                "setup_steps": [_step("setup-1", probe=False)],
                "probe_steps": [_step("probe-1", probe=True)],
            },
            expect_defect=True,
            scheduler=_scheduler(),
        )

    assert [item["kind"] for item in resources] == ["session"] * 4
    assert [item["state"] for item in resources] == [
        "create_intent",
        "created",
        "create_intent",
        "created",
    ]
    assert len(runtime.session_intents) == len(set(runtime.session_intents)) == 2
    assert runtime.activations == [
        ("finance-agent-issue-013-cycle", "1", False),
        ("finance-agent-issue-013-cycle", "1", True),
        ("finance-agent-issue-013-cycle", "1", True),
    ]
    assert runtime.deleted_sessions == [
        ("finance-agent-issue-013-cycle", "session-1"),
        ("finance-agent-issue-013-cycle", "session-2"),
    ]


def test_prepare_hosted_routes_activates_first_exact_version_per_agent() -> None:
    runtime = HostedRuntime()
    runner = FoundryScenarioAttemptRunner(
        runtime,
        endpoint_costs={"issue-013": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=lambda _item: None,
    )
    finance_v1 = _hosted_target()
    finance_v2 = DeployedRuntime(
        **{
            **finance_v1.__dict__,
            "authority_id": "issue-014",
            "runtime_agent_version": "2",
        }
    )
    support_v0 = DeployedRuntime(
        **{
            **finance_v1.__dict__,
            "authority_id": "support-ticket-agent/v0",
            "runtime_kind": "hosted_custom_container",
            "runtime_agent_name": "support-agent-cycle",
            "runtime_agent_version": "1",
        }
    )

    runner.prepare_hosted_routes([finance_v1, finance_v2, support_v0])

    assert runtime.activations == [
        ("finance-agent-issue-013-cycle", "1", False),
        ("support-agent-cycle", "1", False),
    ]


def test_hosted_attempt_defers_failed_session_release_without_losing_evidence() -> None:
    class RuntimeWithFailedRelease(HostedRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.progress = []

        def _delete_hosted_session(self, agent_name, session_id):
            del agent_name, session_id
            raise ContractError("synthetic release failure")

        def report_progress(self, message):
            self.progress.append(message)

    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    runtime = RuntimeWithFailedRelease()
    runner = FoundryScenarioAttemptRunner(
        runtime,
        endpoint_costs={"issue-013": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=lambda _item: None,
        now=lambda: next(times),
    )

    result = runner.run(
        target=_hosted_target(),
        executing_authority_id="issue-013",
        conversation_role="issue",
        scenario={
            "id": "reviewed-path",
            "validation_mode": "deterministic",
            "defect_predicate": {
                "kind": "all_observation_steps_pass",
                "step_ids": ["probe-1"],
                "required_surfaces": ["semantic", "trace"],
            },
        },
        attempt={
            "index": 1,
            "conversation_group": "attempt-1",
            "setup_steps": [_step("setup-1", probe=False)],
            "probe_steps": [_step("probe-1", probe=True)],
        },
        expect_defect=True,
        scheduler=_scheduler(),
    )

    assert result["complete"] is True
    assert runtime.progress == [
        "issue-013: Hosted session release failed after evidence completion; "
        "deferring to cycle cleanup"
    ]
