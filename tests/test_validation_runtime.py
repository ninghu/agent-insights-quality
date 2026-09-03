from __future__ import annotations

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.validation_manifest import authority_specs
from agent_insights_quality.validation_runtime import (
    DeployedRuntime,
    invoke_validation_shard,
    verify_validation_shard,
)


def _contracts():
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    baseline = next(
        item for item in authorities if item.authority_id == "weather-agent/v0"
    )
    issue = next(item for item in authorities if item.authority_id == "issue-001")
    return agents, baseline, issue


def _deployed(authority) -> DeployedRuntime:
    return DeployedRuntime(
        authority_id=authority.authority_id,
        runtime_kind=authority.runtime_kind,
        runtime_agent_name=(
            f"{authority.canonical_agent}-baseline"
            if authority.authority_kind == "baseline"
            else f"{authority.canonical_agent}-{authority.authority_id}"
        ),
        runtime_agent_version="server-version-1",
        provider_agent_id=f"agent/{authority.authority_id}",
        provider_agent_version_id=f"agent/{authority.authority_id}/versions/1",
        provider_content_digest=authority.source_content_digest,
        hosted_identity_id=None,
        hosted_blueprint_id=None,
        hosted_deployment_id=None,
        runtime_principal_id=None,
        telemetry_identity_id=f"agent/{authority.authority_id}/versions/1",
        connection_ids=(),
    )


class _InvokeOnlyRunner:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "started_at": "2026-08-29T12:00:00+00:00",
            "completed_at": "2026-08-29T12:00:01+00:00",
            "response_ids": ["private-response-reference"],
            "usable_results": [True],
            "session_id": None,
        }

    def verify(self, **_kwargs):
        raise AssertionError("invoke-shard must not query or verify telemetry")


class _VerifyOnlyRunner:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, **_kwargs):
        raise AssertionError("verify-shard must not send endpoint traffic")

    def verify(self, **kwargs):
        self.calls.append(kwargs)
        return {"complete": True}


class _BatchVerifyOnlyRunner(_VerifyOnlyRunner):
    def __init__(self, issue_observation_count=None) -> None:
        super().__init__()
        self.issue_observation_count = issue_observation_count

    def verify_attempts(self, **kwargs):
        self.calls.append(kwargs)
        return [
            {
                "complete": True,
                "observation": (
                    False
                    if kwargs["conversation_role"] == "paired_v0"
                    else self.issue_observation_count is None
                    or index <= self.issue_observation_count
                ),
            }
            for index, _ in enumerate(kwargs["attempts"], start=1)
        ]


def test_invoke_shard_sends_issue_and_paired_v0_without_verification() -> None:
    agents, baseline, issue = _contracts()
    runner = _InvokeOnlyRunner()
    result = invoke_validation_shard(
        [issue],
        {
            baseline.authority_id: _deployed(baseline),
            issue.authority_id: _deployed(issue),
        },
        runner=runner,
        scheduler=object(),
        model_contract=agents["models"]["test_agents"],
        paired_baselines={issue.canonical_agent: baseline.authority_id},
    )

    roles = [call["conversation_role"] for call in runner.calls]
    assert roles.count("issue") == roles.count("paired_v0") > 0
    assert result[0]["authority_id"] == issue.authority_id


def test_verify_shard_uses_only_persisted_invocations() -> None:
    agents, baseline, issue = _contracts()
    invocation_runner = _InvokeOnlyRunner()
    invocations = invoke_validation_shard(
        [issue],
        {
            baseline.authority_id: _deployed(baseline),
            issue.authority_id: _deployed(issue),
        },
        runner=invocation_runner,
        scheduler=object(),
        model_contract=agents["models"]["test_agents"],
        paired_baselines={issue.canonical_agent: baseline.authority_id},
    )
    runner = _BatchVerifyOnlyRunner()

    result = verify_validation_shard(
        [issue],
        {
            baseline.authority_id: _deployed(baseline),
            issue.authority_id: _deployed(issue),
        },
        invocations,
        runner=runner,
        scheduler=object(),
        model_contract=agents["models"]["test_agents"],
        validated_commit_sha="a" * 40,
        paired_baselines={issue.canonical_agent: baseline.authority_id},
    )

    roles = [call["conversation_role"] for call in runner.calls]
    assert roles.count("issue") == roles.count("paired_v0") > 0
    assert result[0]["authority_id"] == issue.authority_id


def test_verify_baseline_does_not_require_paired_v0_invocations() -> None:
    agents, baseline, _issue = _contracts()
    invocation_runner = _InvokeOnlyRunner()
    invocations = invoke_validation_shard(
        [baseline],
        {baseline.authority_id: _deployed(baseline)},
        runner=invocation_runner,
        scheduler=object(),
        model_contract=agents["models"]["test_agents"],
        paired_baselines={baseline.canonical_agent: baseline.authority_id},
    )
    runner = _BatchVerifyOnlyRunner()

    result = verify_validation_shard(
        [baseline],
        {baseline.authority_id: _deployed(baseline)},
        invocations,
        runner=runner,
        scheduler=object(),
        model_contract=agents["models"]["test_agents"],
        validated_commit_sha="a" * 40,
        paired_baselines={baseline.canonical_agent: baseline.authority_id},
    )

    assert [call["conversation_role"] for call in runner.calls] == ["baseline"]
    assert result[0]["authority_id"] == baseline.authority_id


def test_issue_verification_batches_once_per_target_and_applies_k_of_n() -> None:
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    issue = next(
        item for item in authorities if item.validation_mode == "model_mediated"
    )
    baseline = next(
        item
        for item in authorities
        if item.authority_id == f"{issue.canonical_agent}/v0"
    )
    invocation_runner = _InvokeOnlyRunner()
    invocations = invoke_validation_shard(
        [issue],
        {
            baseline.authority_id: _deployed(baseline),
            issue.authority_id: _deployed(issue),
        },
        runner=invocation_runner,
        scheduler=object(),
        model_contract=agents["models"]["test_agents"],
        paired_baselines={issue.canonical_agent: baseline.authority_id},
    )
    runner = _BatchVerifyOnlyRunner(issue_observation_count=5)

    result = verify_validation_shard(
        [issue],
        {
            baseline.authority_id: _deployed(baseline),
            issue.authority_id: _deployed(issue),
        },
        invocations,
        runner=runner,
        scheduler=object(),
        model_contract=agents["models"]["test_agents"],
        validated_commit_sha="a" * 40,
        paired_baselines={issue.canonical_agent: baseline.authority_id},
    )[0]

    assert [item["conversation_role"] for item in runner.calls] == [
        "issue",
        "paired_v0",
    ]
    assert result["n"] == 7
    assert result["k"] == 5
    assert result["observation_count"] == 5
    assert result["paired_observation_count"] == 0
    assert result["pass"] is True
