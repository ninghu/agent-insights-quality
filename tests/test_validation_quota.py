from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.live import _rate_limit_values
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_leases import CrossProcessTelemetryLease
from agent_insights_quality.validation_quota import (
    CapacityMeasurement,
    EndpointCost,
    ValidationScheduler,
    WeightedTokenBucket,
    build_capacity_plan,
    validate_capacity_plan,
)


def test_capacity_plan_preserves_percent_and_absolute_headroom() -> None:
    policy = load_validation_policy()
    plan = build_capacity_plan(
        CapacityMeasurement(
            rpm=100,
            tpm=100_000,
            measured_at="2026-08-29T00:00:00Z",
        ),
        policy=policy,
        costs=[
            EndpointCost(requests=2, tokens=4096, inner_model_calls=2),
            EndpointCost(requests=1, tokens=2048, inner_model_calls=1),
        ],
    )
    assert plan.reserved_rpm == 25
    assert plan.reserved_tpm == 25_000
    assert plan.endpoint_concurrency <= 8
    assert plan.provisioning_concurrency == 8
    assert plan.telemetry_query_concurrency == 8
    assert plan.outer_request_envelope == 3
    assert plan.plan_digest.startswith("sha256:")
    validate_capacity_plan(plan, policy=policy)


def test_capacity_preflight_fails_closed_when_headroom_disappears() -> None:
    policy = load_validation_policy()
    with pytest.raises(ContractError, match="preserve reviewed account headroom"):
        build_capacity_plan(
            CapacityMeasurement(
                rpm=8,
                tpm=8192,
                measured_at="2026-08-29T00:00:00Z",
            ),
            policy=policy,
            costs=[EndpointCost(requests=1, tokens=1, inner_model_calls=1)],
        )


def test_inner_model_fanout_consumes_rpm_units() -> None:
    policy = load_validation_policy()
    plan = build_capacity_plan(
        CapacityMeasurement(
            rpm=12,
            tpm=100_000,
            measured_at="2026-08-29T00:00:00Z",
        ),
        policy=policy,
        costs=[EndpointCost(requests=4, tokens=100, inner_model_calls=4)],
    )
    assert plan.reserved_rpm == 8
    assert plan.available_rpm == 4
    assert plan.endpoint_concurrency == 1
    assert plan.outer_request_envelope == 4
    with pytest.raises(ContractError, match="reviewed envelope"):
        build_capacity_plan(
            CapacityMeasurement(
                rpm=100,
                tpm=100_000,
                measured_at="2026-08-29T00:00:00Z",
            ),
            policy=policy,
            costs=[
                EndpointCost(
                    requests=1,
                    tokens=100,
                    inner_model_calls=policy.limits.inner_model_call_limit + 1,
                )
            ],
        )


def test_weighted_bucket_waits_and_honors_rate_limit_reduction() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleeper(value: float) -> None:
        sleeps.append(value)
        now[0] += value

    bucket = WeightedTokenBucket(
        request_capacity=2,
        token_capacity=200,
        clock=clock,
        sleeper=sleeper,
    )
    cost = EndpointCost(requests=2, tokens=200, inner_model_calls=1)
    bucket.acquire(cost)
    bucket.acquire(cost)
    assert sleeps == [pytest.approx(60.0)]

    retry_bucket = WeightedTokenBucket(
        request_capacity=2,
        token_capacity=200,
        clock=clock,
        sleeper=sleeper,
    )
    retry_bucket.reduce_from_rate_limit(
        remaining_requests=2,
        remaining_tokens=200,
        retry_after_seconds=3,
    )
    retry_bucket.acquire(cost)
    assert sleeps[-1] == 3


def test_retry_after_blocks_every_bucket_consumer() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleeper(value: float) -> None:
        sleeps.append(value)
        now[0] += value

    bucket = WeightedTokenBucket(
        request_capacity=10,
        token_capacity=1000,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    bucket.reduce_from_rate_limit(
        remaining_requests=10,
        remaining_tokens=1000,
        retry_after_seconds=7,
    )
    bucket.acquire(EndpointCost(requests=1, tokens=1, inner_model_calls=1))
    assert sleeps == [7]


def test_scheduler_enforces_shared_telemetry_query_limit() -> None:
    policy = load_validation_policy()
    plan = build_capacity_plan(
        CapacityMeasurement(
            rpm=100,
            tpm=100_000,
            measured_at="2026-08-29T00:00:00Z",
        ),
        policy=policy,
        costs=[EndpointCost(requests=1, tokens=100, inner_model_calls=1)],
    )
    scheduler = ValidationScheduler(
        plan,
        WeightedTokenBucket(request_capacity=100, token_capacity=10000),
    )
    active = 0
    maximum = 0
    lock = threading.Lock()

    def query() -> None:
        nonlocal active, maximum
        with scheduler.telemetry_query():
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1

    threads = [threading.Thread(target=query) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert 1 < maximum <= plan.telemetry_query_concurrency


def test_policy_does_not_raise_concurrency_from_runtime_headers() -> None:
    policy = load_validation_policy()
    smaller = replace(
        policy,
        limits=replace(policy.limits, provisioning_concurrency=2),
    )
    plan = build_capacity_plan(
        CapacityMeasurement(
            rpm=1000,
            tpm=1_000_000,
            measured_at="2026-08-29T00:00:00Z",
        ),
        policy=smaller,
        costs=[EndpointCost(requests=1, tokens=100, inner_model_calls=1)],
    )
    assert plan.endpoint_concurrency == 2


def test_capacity_plan_digest_prevents_runtime_concurrency_tampering() -> None:
    policy = load_validation_policy()
    plan = build_capacity_plan(
        CapacityMeasurement(
            rpm=100,
            tpm=100_000,
            measured_at="2026-08-29T00:00:00Z",
        ),
        policy=policy,
        costs=[EndpointCost(requests=1, tokens=100, inner_model_calls=1)],
    )
    with pytest.raises(ContractError, match="digest is stale"):
        validate_capacity_plan(
            replace(plan, endpoint_concurrency=plan.endpoint_concurrency - 1),
            policy=policy,
        )


def test_cross_process_telemetry_lease_enforces_eight_fenced_slots(
    tmp_path,
) -> None:
    policy = load_validation_policy()
    plan = build_capacity_plan(
        CapacityMeasurement(
            rpm=100,
            tpm=100_000,
            measured_at="2026-08-29T00:00:00Z",
        ),
        policy=policy,
        costs=[EndpointCost(requests=1, tokens=1024, inner_model_calls=1)],
    )
    leases = [
        CrossProcessTelemetryLease(
            run_id="validation-0123456789ab",
            capacity=plan,
            fence=lambda: None,
            root=tmp_path,
        )
        for _ in range(9)
    ]
    try:
        assert all(lease.try_acquire() for lease in leases[:8])
        assert {lease.slot for lease in leases[:8]} == set(range(1, 9))
        assert leases[8].try_acquire() is False
        leases[0].release()
        assert leases[8].try_acquire() is True
        assert leases[8].slot == 1
    finally:
        for lease in leases:
            lease.release()


def test_runtime_rate_limit_headers_are_reduced_to_public_numeric_feedback() -> None:
    assert _rate_limit_values(
        {
            "x-ratelimit-remaining-requests": "17",
            "x-ratelimit-remaining-tokens": "4096",
            "Retry-After": "2.5",
        }
    ) == {
        "remaining_requests": 17,
        "remaining_tokens": 4096,
        "retry_after_seconds": 2.5,
    }
    assert _rate_limit_values(
        {
            "x-ratelimit-remaining-requests": "private-invalid",
            "Retry-After": "-1",
        }
    ) == {
        "remaining_requests": None,
        "remaining_tokens": None,
        "retry_after_seconds": None,
    }
