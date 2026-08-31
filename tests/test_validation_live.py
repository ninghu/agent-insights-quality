from __future__ import annotations

from datetime import UTC, datetime

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
    ) -> None:
        self.assertion_pass = assertion_pass
        self.identity_pass = identity_pass
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
    @staticmethod
    def _activate_hosted_version(agent_name, foundry_version):
        del agent_name, foundry_version

    @staticmethod
    def _create_hosted_session(
        agent_name,
        foundry_version,
        *,
        validation_intent_reference,
    ):
        del agent_name, foundry_version
        assert validation_intent_reference.startswith("sha256:")
        return "session-synthetic"

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


def test_hosted_attempt_journals_only_persistent_session() -> None:
    times = iter(
        [
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, 1, tzinfo=UTC),
        ]
    )
    resources = []
    runner = FoundryScenarioAttemptRunner(
        HostedRuntime(),
        endpoint_costs={"issue-013": EndpointCost(1, 10, 1)},
        stabilization_seconds=1,
        record_resource=resources.append,
        now=lambda: next(times),
    )
    runner.run(
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
    assert [item["kind"] for item in resources] == ["session", "session"]
