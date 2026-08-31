from __future__ import annotations

import threading
import time
from copy import deepcopy

import pytest

from agent_insights_quality.live import RemoteOperationError
from agent_insights_quality.provisioning import RemoteHttpError
from agent_insights_quality.util import (
    ContractError,
    SharedRuntimeError,
    content_hash,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_quota import (
    CapacityPlan,
    EndpointCost,
    ValidationScheduler,
    WeightedTokenBucket,
)
from agent_insights_quality.validation_rules import (
    CONVERSATION_PLACEHOLDER,
    RUNTIME_AGENT_NAME_PLACEHOLDER,
    RUNTIME_AGENT_VERSION_PLACEHOLDER,
    stamp_execution_digests,
)
from agent_insights_quality.validation_runtime import (
    AgentDeploymentIncomplete,
    AgentExecutionIncomplete,
    AuthoritySpec,
    DeployedRuntime,
    _agent_failure_summary,
    _deployment_canaries,
    deploy_all_authorities,
    execute_validation_matrix,
    execute_validation_phase,
    invalidated_authorities,
    opaque_cycle_suffix,
    plan_runtime_topology,
    validation_project_name,
)

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40
MODEL = {"id": "gpt-5.4-mini", "version": "2026-03-17"}


def _step(step_id: str, text: str, *, assertions: bool) -> dict:
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
                        "content": [{"type": "input_text", "text": text}],
                    }
                ],
                "conversation": {"id": CONVERSATION_PLACEHOLDER},
            },
        },
        "expected": {
            "http_status": 200,
            "semantic_assertions": (
                {"required_terms_all": ["synthetic"]} if assertions else {}
            ),
            "trace_assertions": [],
            "identity_assertions": {
                "agent_name": RUNTIME_AGENT_NAME_PLACEHOLDER,
                "agent_version": RUNTIME_AGENT_VERSION_PLACEHOLDER,
            },
        },
    }


def _authority(
    authority_id: str,
    agent: str,
    *,
    baseline: bool,
    model_mediated: bool = False,
    runtime_kind: str = "hosted_code",
) -> AuthoritySpec:
    mode = "baseline" if baseline else (
        "model_mediated" if model_mediated else "deterministic"
    )
    n = 7 if mode == "model_mediated" else 5
    scenario = {
        "id": "reviewed-path",
        "validation_mode": mode,
        "n": n,
        "k": 5,
        "fixtures": [],
        "attempts": [
            {
                "index": index,
                "conversation_group": f"{authority_id}-{index}",
                "parameters": {"case": index},
                "setup_steps": [
                    _step(
                        f"{authority_id}-setup-{index}",
                        f"Acknowledge synthetic setup {index}.",
                        assertions=False,
                    )
                ],
                "probe_steps": [
                    _step(
                        f"{authority_id}-probe-{index}",
                        f"Evaluate synthetic case {index}.",
                        assertions=True,
                    )
                ],
            }
            for index in range(1, n + 1)
        ],
        "healthy_predicate": (
            {"kind": "all_probe_assertions_pass"} if baseline else None
        ),
        "defect_predicate": (
            {"kind": "never"}
            if baseline
            else {
                "kind": "all_observation_steps_pass",
                "step_ids": [
                    f"{authority_id}-probe-{index}"
                    for index in range(1, n + 1)
                ],
                "required_surfaces": ["semantic"],
            }
        ),
        "v0_control_predicate": (
            None if baseline else {"kind": "zero_defect_observations"}
        ),
    }
    rules = stamp_execution_digests(
        {"schema_version": "1.0.0", "scenarios": [scenario]},
        authority_id=authority_id,
        authority_kind="baseline" if baseline else "issue",
        canonical_agent=agent,
        logical_version="v0" if baseline else authority_id,
        runtime_kind=runtime_kind,
        framework="prompt" if runtime_kind == "prompt" else "langgraph",
        model_contract=MODEL,
    )
    return AuthoritySpec(
        authority_id=authority_id,
        authority_kind="baseline" if baseline else "issue",
        canonical_agent=agent,
        logical_version="v0" if baseline else authority_id,
        runtime_kind=runtime_kind,
        framework="prompt" if runtime_kind == "prompt" else "langgraph",
        source_content_digest=HASH,
        execution_digest=rules["execution_digest"],
        validation_mode=mode,
        validation_rules=rules,
    )


def _authorities() -> list[AuthoritySpec]:
    agents = [
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    ]
    values = [
        _authority(
            f"{agent}/v0",
            agent,
            baseline=True,
            runtime_kind="prompt" if agent == "weather-agent" else "hosted_code",
        )
        for agent in agents
    ]
    for number in range(1, 37):
        agent = (
            "weather-agent"
            if number <= 6
            else "healthcare-agent"
            if number <= 12
            else "finance-agent"
            if number <= 20
            else "travel-agent"
            if number <= 28
            else "support-ticket-agent"
        )
        values.append(
            _authority(
                f"issue-{number:03d}",
                agent,
                baseline=False,
                model_mediated=number <= 12 or number in {21, 25, 26},
                runtime_kind="prompt" if agent == "weather-agent" else "hosted_code",
            )
        )
    return values


class Deployer:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()
        self.ready: list[str] = []
        self.started: list[str] = []

    def deploy(self, authority, planned) -> DeployedRuntime:
        with self.lock:
            self.started.append(authority.authority_id)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        time.sleep(0.001)
        with self.lock:
            self.active -= 1
        hosted = authority.runtime_kind != "prompt"
        return DeployedRuntime(
            authority_id=authority.authority_id,
            runtime_kind=authority.runtime_kind,
            runtime_agent_name=planned.runtime_agent_name,
            runtime_agent_version="1",
            provider_agent_id=f"agent-{authority.authority_id}",
            provider_agent_version_id=f"version-{authority.authority_id}",
            hosted_identity_id=(
                f"identity-{authority.authority_id}" if hosted else None
            ),
            hosted_blueprint_id=(
                f"blueprint-{authority.authority_id}" if hosted else None
            ),
            hosted_deployment_id=(
                f"deployment-{authority.authority_id}" if hosted else None
            ),
            runtime_principal_id=(
                f"principal-{authority.authority_id}" if hosted else None
            ),
            telemetry_identity_id=f"version-{authority.authority_id}",
            connection_ids=(),
        )

    def assert_ready(self, authority, _deployed) -> None:
        self.ready.append(authority.authority_id)


class Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, bool, str]] = []

    def run(
        self,
        *,
        target,
        executing_authority_id,
        conversation_role,
        scenario,
        attempt,
        expect_defect,
        scheduler,
    ):
        scenario_id = scenario["id"]
        execution = {
            "executing_authority_id": executing_authority_id,
            "target_authority_id": target.authority_id,
            "conversation_role": conversation_role,
            "scenario_id": scenario_id,
            "attempt": attempt["index"],
        }
        self.calls.append(
            (
                target.authority_id,
                attempt["index"],
                expect_defect,
                content_hash(attempt),
            )
        )
        step = {
            "index": 1,
            "step_id": attempt["probe_steps"][0]["id"],
            "request_digest": content_hash(attempt["probe_steps"][0]["request"]),
            "response_reference": content_hash(
                {"response": execution, "step": "probe"}
            ),
            "operation_reference": content_hash(
                {"operation": execution, "step": "probe"}
            ),
            "complete": True,
            "endpoint_pass": True,
            "semantic_pass": expect_defect,
            "trace_pass": True,
            "identity_pass": True,
        }
        setup = {**step, "semantic_pass": True}
        setup["step_id"] = attempt["setup_steps"][0]["id"]
        setup["request_digest"] = content_hash(attempt["setup_steps"][0]["request"])
        setup["response_reference"] = content_hash(
            {"response": execution, "step": "setup"}
        )
        setup["operation_reference"] = content_hash(
            {"operation": execution, "step": "setup"}
        )
        scheduler.acquire_request(
            EndpointCost(requests=1, tokens=10, inner_model_calls=1)
        )
        return {
            "index": attempt["index"],
            "conversation_reference": content_hash(
                {"conversation": execution}
            ),
            "session_reference": content_hash(
                {"session": execution}
            ),
            "response_references": [
                setup["response_reference"],
                step["response_reference"],
            ],
            "operation_references": [
                setup["operation_reference"],
                step["operation_reference"],
            ],
            "setup_steps": [setup],
            "probe_steps": [step],
            "complete": True,
            "defect_observed": expect_defect,
            "expected_observation_pass": True,
            "error_code": None,
        }


def _scheduler() -> ValidationScheduler:
    plan = CapacityPlan(
        measured_rpm=100,
        measured_tpm=100000,
        measured_at="2026-08-29T00:00:00Z",
        reserved_percent=25,
        reserved_rpm=25,
        reserved_tpm=25000,
        available_rpm=75,
        available_tpm=75000,
        outer_request_envelope=500,
        worst_case_inner_model_calls=1,
        worst_case_inner_tokens=10,
        endpoint_concurrency=8,
        provisioning_concurrency=8,
        telemetry_query_concurrency=4,
        runtime_attempt_concurrency=1,
        inner_model_call_limit=4,
        plan_digest=HASH,
    )
    return ValidationScheduler(
        plan,
        WeightedTokenBucket(request_capacity=1000, token_capacity=100000),
    )


def test_names_are_opaque_bounded_deterministic_and_collision_free() -> None:
    policy = load_validation_policy()
    suffix = opaque_cycle_suffix(
        repository="ninghu/agent-insights-quality",
        pr_number=999,
        commit_sha=HEAD,
        run_id="run-1",
    )
    assert len(suffix) == 12
    assert validation_project_name(suffix, policy=policy).startswith(
        "aiq-validation-"
    )
    topology = plan_runtime_topology(
        _authorities(),
        cycle_suffix=suffix,
        policy=policy,
    )
    assert len(topology) == len({item.runtime_agent_name for item in topology}) == 41
    assert all(policy.agent_name_policy.accepts(item.runtime_agent_name) for item in topology)


def test_deployment_is_bounded_to_eight_and_each_authority_is_independent() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    deployer = Deployer()
    events = []
    deployed = deploy_all_authorities(
        authorities,
        planned,
        deployer=deployer,
        maximum_concurrency=8,
        record_resource=events.append,
    )
    assert len(deployed) == 41
    assert 1 < deployer.maximum <= 8
    prompt_canary, hosted_canary = _deployment_canaries(authorities)
    assert set(deployer.started[:2]) == {
        prompt_canary.authority_id,
        hosted_canary.authority_id,
    }
    assert set(deployer.ready) == {
        prompt_canary.authority_id,
        hosted_canary.authority_id,
    }
    by_intent = {}
    for index, event in enumerate(events):
        key = event["intent_reference"]
        if event["state"] == "create_intent":
            assert event["runtime_kind"] in {
                "prompt",
                "hosted_code",
                "hosted_custom_container",
            }
            assert event["discovery_key"]
            by_intent[key] = index
        elif event["state"] == "created":
            assert by_intent[key] < index


def test_failed_hosted_canary_records_cleanup_intents_without_fanout() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    prompt_canary, hosted_canary = _deployment_canaries(authorities)
    events = []

    class FailingHostedCanary(Deployer):
        def deploy(self, authority, planned):
            if authority.authority_id == hosted_canary.authority_id:
                self.started.append(authority.authority_id)
                raise ContractError("synthetic Hosted canary failure")
            return super().deploy(authority, planned)

    deployer = FailingHostedCanary()
    with pytest.raises(AgentDeploymentIncomplete):
        deploy_all_authorities(
            authorities,
            planned,
            deployer=deployer,
            maximum_concurrency=8,
            record_resource=events.append,
        )
    assert set(deployer.started) == {
        prompt_canary.authority_id,
        hosted_canary.authority_id,
    }
    assert deployer.ready == [prompt_canary.authority_id]
    hosted_events = [
        item
        for item in events
        if item["authority_id"] == hosted_canary.authority_id
    ]
    assert {item["state"] for item in hosted_events} == {
        "create_intent",
        "ambiguous_create",
    }
    assert not any(
        item["authority_id"]
        not in {prompt_canary.authority_id, hosted_canary.authority_id}
        for item in events
    )


def test_prompt_readiness_failure_prevents_hosted_and_remaining_fanout() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    prompt_canary, hosted_canary = _deployment_canaries(authorities)
    events = []

    class FailingPromptReadiness(Deployer):
        def assert_ready(self, authority, deployed) -> None:
            super().assert_ready(authority, deployed)
            if authority.authority_id == prompt_canary.authority_id:
                raise ContractError("synthetic Prompt readiness failure")

    deployer = FailingPromptReadiness()
    with pytest.raises(AgentDeploymentIncomplete):
        deploy_all_authorities(
            authorities,
            planned,
            deployer=deployer,
            maximum_concurrency=8,
            record_resource=events.append,
        )
    assert set(deployer.started) == {
        prompt_canary.authority_id,
        hosted_canary.authority_id,
    }
    assert {
        item["state"]
        for item in events
        if item["authority_id"] == prompt_canary.authority_id
    } == {"create_intent", "created"}
    assert not any(
        item["authority_id"]
        not in {prompt_canary.authority_id, hosted_canary.authority_id}
        for item in events
    )


def test_transient_subset_failure_retries_only_unresolved_authority() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    target = next(
        item for item in authorities if item.authority_id == "issue-013"
    )
    attempts = {}
    recoveries = []
    ready = []

    class FlakyDeployer(Deployer):
        def deploy(self, authority, planned):
            attempts[authority.authority_id] = (
                attempts.get(authority.authority_id, 0) + 1
            )
            if (
                authority.authority_id == target.authority_id
                and attempts[authority.authority_id] == 1
            ):
                self.started.append(authority.authority_id)
                raise RemoteHttpError(
                    503,
                    "ServiceUnavailable",
                    "Synthetic transient",
                    "POST /agents",
                )
            return super().deploy(authority, planned)

    deployed = deploy_all_authorities(
        authorities,
        planned,
        deployer=FlakyDeployer(),
        maximum_concurrency=8,
        record_ready=lambda authority, _runtime: ready.append(
            authority.authority_id
        ),
        record_recovery=lambda authority, state, count, code: (
            recoveries.append(
                (authority.authority_id, state, count, code)
            )
        ),
    )
    assert len(deployed) == 41
    assert attempts[target.authority_id] == 2
    assert all(
        count == 1
        for authority_id, count in attempts.items()
        if authority_id != target.authority_id
    )
    assert recoveries == [
        (
            target.authority_id,
            "failed",
            1,
            "transient_provider_error",
        )
    ]
    assert len(ready) == 41


def test_resume_does_not_mutate_or_redeploy_ready_authorities() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    initial = deploy_all_authorities(
        authorities,
        planned,
        deployer=Deployer(),
        maximum_concurrency=8,
    )
    prompt_canary, hosted_canary = _deployment_canaries(authorities)
    retained_ids = {
        prompt_canary.authority_id,
        hosted_canary.authority_id,
        "issue-013",
    }
    retained = {
        authority_id: initial[authority_id]
        for authority_id in retained_ids
    }
    resumed_deployer = Deployer()
    resumed = deploy_all_authorities(
        authorities,
        planned,
        deployer=resumed_deployer,
        maximum_concurrency=8,
        existing_deployed=retained,
    )
    assert not retained_ids.intersection(resumed_deployer.started)
    assert all(resumed[key] is value for key, value in retained.items())
    assert set(resumed_deployer.ready) == {
        prompt_canary.authority_id,
        hosted_canary.authority_id,
    }


def test_transient_canary_retry_exhaustion_fails_before_fanout() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    prompt_canary, hosted_canary = _deployment_canaries(authorities)
    recoveries = []

    class ExhaustedCanary(Deployer):
        def deploy(self, authority, planned):
            self.started.append(authority.authority_id)
            raise RemoteHttpError(
                503,
                "ServiceUnavailable",
                "Synthetic transient",
                "POST /agents",
            )

    deployer = ExhaustedCanary()
    with pytest.raises(AgentDeploymentIncomplete):
        deploy_all_authorities(
            authorities,
            planned,
            deployer=deployer,
            maximum_concurrency=8,
            record_recovery=lambda authority, state, count, code: (
                recoveries.append(
                    (authority.authority_id, state, count, code)
                )
            ),
        )
    assert deployer.started.count(prompt_canary.authority_id) == 4
    assert deployer.started.count(hosted_canary.authority_id) == 4
    assert not any(
        authority_id
        not in {prompt_canary.authority_id, hosted_canary.authority_id}
        for authority_id in deployer.started
    )
    assert sorted(
        item[2]
        for item in recoveries
        if item[0] == prompt_canary.authority_id
    ) == [1, 2, 3]
    assert sorted(
        item[2]
        for item in recoveries
        if item[0] == hosted_canary.authority_id
    ) == [1, 2, 3]


def test_deterministic_deployment_failure_isolated_to_agent_lane() -> None:
    policy = load_validation_policy()
    all_authorities = _authorities()
    authorities = [
        item
        for item in all_authorities
        if item.authority_id in {
            "issue-013",
            "issue-014",
            "issue-021",
            "issue-022",
        }
    ]
    planned = [
        item
        for item in plan_runtime_topology(
            all_authorities,
            cycle_suffix="0123456789ab",
            policy=policy,
        )
        if item.authority_id in {
            authority.authority_id for authority in authorities
        }
    ]

    class FailingFinanceDeployer(Deployer):
        def deploy(self, authority, planned):
            if authority.authority_id == "issue-013":
                self.started.append(authority.authority_id)
                raise ContractError("synthetic deterministic failure")
            return super().deploy(authority, planned)

    deployer = FailingFinanceDeployer()
    failures = []
    with pytest.raises(AgentDeploymentIncomplete) as caught:
        deploy_all_authorities(
            authorities,
            planned,
            deployer=deployer,
            maximum_concurrency=8,
            require_architecture_canaries=False,
            record_failure=failures.append,
        )
    assert caught.value.failures == failures
    assert [item["canonical_agent"] for item in failures] == [
        "finance-agent"
    ]
    assert "issue-014" not in deployer.started
    assert {"issue-021", "issue-022"}.issubset(deployer.started)


def test_phase_two_counts_prior_phase_recovered_versions() -> None:
    policy = load_validation_policy()
    all_authorities = _authorities()
    authority = next(
        item for item in all_authorities if item.authority_id == "issue-013"
    )
    planned = [
        item
        for item in plan_runtime_topology(
            all_authorities,
            cycle_suffix="0123456789ab",
            policy=policy,
        )
        if item.authority_id == authority.authority_id
    ]

    class TransientDeployer(Deployer):
        def deploy(self, authority, planned):
            self.started.append(authority.authority_id)
            raise RemoteHttpError(
                503,
                "ServiceUnavailable",
                "Synthetic transient",
                "POST /agents",
            )

    recoveries = []
    with pytest.raises(AgentDeploymentIncomplete) as caught:
        deploy_all_authorities(
            [authority],
            planned,
            deployer=TransientDeployer(),
            maximum_concurrency=8,
            require_architecture_canaries=False,
            prior_recovered_authorities={
                "finance-agent": [
                    "finance-agent/v0",
                    "issue-014",
                    "issue-015",
                ]
            },
            record_recovery=lambda *args: recoveries.append(args),
        )
    assert caught.value.failures[0]["error_code"] == "recovery_exhausted"
    assert recoveries == []


def test_all_issues_run_exact_same_matrix_against_paired_v0_without_resampling() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    deployed = deploy_all_authorities(
        authorities,
        planned,
        deployer=Deployer(),
        maximum_concurrency=8,
    )
    runner = Runner()
    results = execute_validation_matrix(
        authorities,
        deployed,
        runner=runner,
        scheduler=_scheduler(),
        model_contract=MODEL,
        validated_commit_sha=HEAD,
    )
    assert len(results) == 41
    assert all(result["pass"] for result in results)
    for authority in authorities:
        calls = [
            call for call in runner.calls if call[0] == authority.authority_id
        ]
        expected = 5 if authority.authority_kind == "baseline" else (
            7 if authority.validation_rules["scenarios"][0]["validation_mode"] == "model_mediated" else 5
        )
        assert len(calls) >= expected
        if authority.authority_kind == "issue":
            baseline_id = f"{authority.canonical_agent}/v0"
            issue_calls = [
                call for call in runner.calls if call[0] == authority.authority_id
            ]
            control_calls = [
                call
                for call in runner.calls
                if call[0] == baseline_id
                and call[2] is False
                and call[3] in {item[3] for item in issue_calls}
            ]
            assert len(issue_calls) == len(control_calls) == expected


def test_agent_traffic_failure_does_not_cancel_other_agent_lanes() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    deployed = deploy_all_authorities(
        authorities,
        planned,
        deployer=Deployer(),
        maximum_concurrency=8,
    )

    class AcceptedFailure(ContractError):
        request_accepted = True

    class FailingRunner(Runner):
        def __init__(self) -> None:
            super().__init__()
            self.failure_calls = 0

        def run(self, **kwargs):
            if kwargs["executing_authority_id"] == "issue-013":
                self.failure_calls += 1
                raise AcceptedFailure("synthetic Agent failure")
            return super().run(**kwargs)

    failures = []
    runner = FailingRunner()
    with pytest.raises(AgentExecutionIncomplete) as caught:
        execute_validation_matrix(
            authorities,
            deployed,
            runner=runner,
            scheduler=_scheduler(),
            model_contract=MODEL,
            validated_commit_sha=HEAD,
            record_failure=failures.append,
        )
    assert caught.value.failures == failures
    assert failures == [
        {
            "canonical_agent": "finance-agent",
            "authority_id": "issue-013",
            "stage": "traffic",
            "error_code": "accepted_failure",
            "request_accepted": True,
        }
    ]
    completed_agents = {
        item["canonical_agent"]
        for item in caught.value.partial_results
    }
    assert completed_agents == {
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    }
    assert not any(
        call[0] == "issue-013" for call in runner.calls
    )
    assert runner.failure_calls == 1
    assert any(call[0] == "issue-036" for call in runner.calls)


@pytest.mark.parametrize(
    ("code", "request_accepted"),
    [
        ("prompt_response_identity_missing", True),
        ("too_many_requests", False),
        ("remote_no_response", None),
    ],
)
def test_traffic_failure_summary_preserves_request_acceptance(
    code,
    request_accepted,
) -> None:
    authority = next(
        item for item in _authorities() if item.authority_id == "weather-agent/v0"
    )
    error = RemoteOperationError(
        "Synthetic public-safe traffic failure",
        code=code,
        status=None,
        request_accepted=request_accepted,
    )

    assert _agent_failure_summary(
        authority,
        stage="traffic",
        error=error,
        request_accepted=error.request_accepted,
    ) == {
        "canonical_agent": "weather-agent",
        "authority_id": "weather-agent/v0",
        "stage": "traffic",
        "error_code": code,
        "request_accepted": request_accepted,
    }


def test_traffic_failure_summary_preserves_safe_correlation_counts() -> None:
    authority = next(
        item for item in _authorities() if item.authority_id == "weather-agent/v0"
    )

    class CorrelationFailure(ContractError):
        code = "telemetry_correlation_timeout"
        request_accepted = True
        matched_reference_count = 1
        expected_reference_count = 2
        missing_reference_count = 1

    error = CorrelationFailure("Synthetic public-safe telemetry failure")

    assert _agent_failure_summary(
        authority,
        stage="traffic",
        error=error,
        request_accepted=error.request_accepted,
    ) == {
        "canonical_agent": "weather-agent",
        "authority_id": "weather-agent/v0",
        "stage": "traffic",
        "error_code": "telemetry_correlation_timeout",
        "request_accepted": True,
        "matched_reference_count": 1,
        "expected_reference_count": 2,
        "missing_reference_count": 1,
    }


def test_shared_traffic_failure_aborts_globally() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    deployed = deploy_all_authorities(
        authorities,
        planned,
        deployer=Deployer(),
        maximum_concurrency=8,
    )

    class SharedFailureRunner(Runner):
        def run(self, **kwargs):
            if kwargs["executing_authority_id"] == "weather-agent/v0":
                raise SharedRuntimeError("synthetic shared failure")
            return super().run(**kwargs)

    with pytest.raises(SharedRuntimeError, match="shared failure"):
        execute_validation_matrix(
            authorities,
            deployed,
            runner=SharedFailureRunner(),
            scheduler=_scheduler(),
            model_contract=MODEL,
            validated_commit_sha=HEAD,
        )


def test_phase_two_uses_retained_canary_baselines_for_paired_controls() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    deployed = deploy_all_authorities(
        authorities,
        planned,
        deployer=Deployer(),
        maximum_concurrency=8,
    )
    canary_ids = {"weather-agent/v0", "finance-agent/v0"}
    phase_two = [
        item for item in authorities if item.authority_id not in canary_ids
    ]
    runner = Runner()
    results = execute_validation_phase(
        phase_two,
        deployed,
        runner=runner,
        scheduler=_scheduler(),
        model_contract=MODEL,
        validated_commit_sha=HEAD,
        paired_baselines={
            item.canonical_agent: item.authority_id
            for item in authorities
            if item.authority_kind == "baseline"
        },
    )
    assert len(results) == 39
    assert any(
        call[0] == "weather-agent/v0" and call[2] is False
        for call in runner.calls
    )
    assert any(
        call[0] == "finance-agent/v0" and call[2] is False
        for call in runner.calls
    )


def test_phase_one_baseline_traffic_runs_both_agents_concurrently() -> None:
    policy = load_validation_policy()
    authorities = _authorities()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix="0123456789ab",
        policy=policy,
    )
    deployed = deploy_all_authorities(
        authorities,
        planned,
        deployer=Deployer(),
        maximum_concurrency=8,
    )
    phase_one = [
        item
        for item in authorities
        if item.authority_id in {
            "weather-agent/v0",
            "finance-agent/v0",
        }
    ]

    class ConcurrentRunner(Runner):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def run(self, **kwargs):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.005)
            try:
                return super().run(**kwargs)
            finally:
                with self.lock:
                    self.active -= 1

    runner = ConcurrentRunner()
    results = execute_validation_phase(
        phase_one,
        deployed,
        runner=runner,
        scheduler=_scheduler(),
        model_contract=MODEL,
        validated_commit_sha=HEAD,
        paired_baselines={
            "weather-agent": "weather-agent/v0",
            "finance-agent": "finance-agent/v0",
        },
    )
    assert len(results) == 2
    assert runner.maximum == 2


def test_invalidation_has_no_cross_cycle_cache_and_shared_changes_invalidate_all() -> None:
    current = {f"authority-{index}": content_hash(index) for index in range(41)}
    assert invalidated_authorities(
        current_cycle_id="cycle-2",
        previous_cycle_id="cycle-1",
        previous_contract_digest=HASH,
        current_contract_digest=HASH,
        previous_source_digests=current,
        current_source_digests=current,
    ) == set(current)
    assert invalidated_authorities(
        current_cycle_id="cycle-1",
        previous_cycle_id="cycle-1",
        previous_contract_digest=HASH,
        current_contract_digest=content_hash("changed"),
        previous_source_digests=current,
        current_source_digests=current,
    ) == set(current)
    changed = deepcopy(current)
    changed["authority-4"] = content_hash("new source")
    assert invalidated_authorities(
        current_cycle_id="cycle-1",
        previous_cycle_id="cycle-1",
        previous_contract_digest=HASH,
        current_contract_digest=HASH,
        previous_source_digests=current,
        current_source_digests=changed,
    ) == {"authority-4"}
