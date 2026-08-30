from __future__ import annotations

import threading
import time
from copy import deepcopy

from agent_insights_quality.util import content_hash
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
    AuthoritySpec,
    DeployedRuntime,
    deploy_all_authorities,
    execute_validation_matrix,
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
        runtime_kind="hosted_code",
        framework="langgraph",
        model_contract=MODEL,
    )
    return AuthoritySpec(
        authority_id=authority_id,
        authority_kind="baseline" if baseline else "issue",
        canonical_agent=agent,
        logical_version="v0" if baseline else authority_id,
        runtime_kind="hosted_code",
        framework="langgraph",
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
        _authority(f"{agent}/v0", agent, baseline=True)
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
            )
        )
    return values


class Deployer:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def deploy(self, authority, planned) -> DeployedRuntime:
        with self.lock:
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


class Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, bool, str]] = []

    def run(
        self,
        *,
        target,
        scenario,
        attempt,
        expect_defect,
        scheduler,
    ):
        del scenario
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
            "request_digest": content_hash(attempt["probe_steps"][0]["request"]),
            "response_reference": content_hash(
                {"target": target.authority_id, "attempt": attempt["index"]}
            ),
            "operation_reference": content_hash(
                {"operation": target.authority_id, "attempt": attempt["index"]}
            ),
            "complete": True,
            "endpoint_pass": True,
            "semantic_pass": expect_defect,
            "trace_pass": True,
            "identity_pass": True,
        }
        setup = {**step, "semantic_pass": True}
        setup["request_digest"] = content_hash(attempt["setup_steps"][0]["request"])
        with scheduler.attempt(
            target.authority_id,
            EndpointCost(requests=1, tokens=10, inner_model_calls=1),
        ):
            pass
        return {
            "index": attempt["index"],
            "conversation_reference": content_hash(
                {"conversation": target.authority_id, "attempt": attempt["index"]}
            ),
            "session_reference": content_hash(
                {"session": target.authority_id, "attempt": attempt["index"]}
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
        candidate_head_sha=HEAD,
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
    by_intent = {}
    for index, event in enumerate(events):
        key = event["intent_reference"]
        if event["state"] == "create_intent":
            by_intent[key] = index
        elif event["state"] == "created":
            assert by_intent[key] < index


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
        validated_head_sha=HEAD,
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
