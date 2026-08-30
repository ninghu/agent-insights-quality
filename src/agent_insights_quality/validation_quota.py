from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Callable, Iterator

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_policy import ValidationPolicy


@dataclass(frozen=True)
class EndpointCost:
    requests: int
    tokens: int
    inner_model_calls: int


@dataclass(frozen=True)
class CapacityMeasurement:
    rpm: int
    tpm: int
    measured_at: str


@dataclass(frozen=True)
class CapacityPlan:
    measured_rpm: int
    measured_tpm: int
    measured_at: str
    reserved_percent: int
    reserved_rpm: int
    reserved_tpm: int
    available_rpm: int
    available_tpm: int
    outer_request_envelope: int
    worst_case_inner_model_calls: int
    worst_case_inner_tokens: int
    endpoint_concurrency: int
    provisioning_concurrency: int
    telemetry_query_concurrency: int
    runtime_attempt_concurrency: int
    inner_model_call_limit: int
    plan_digest: str


def build_capacity_plan(
    measurement: CapacityMeasurement,
    *,
    policy: ValidationPolicy,
    costs: list[EndpointCost],
) -> CapacityPlan:
    if not costs:
        raise ContractError("Validation capacity plan requires endpoint costs")
    if (
        isinstance(measurement.rpm, bool)
        or isinstance(measurement.tpm, bool)
        or measurement.rpm <= 0
        or measurement.tpm <= 0
    ):
        raise ContractError("Measured validation capacity must be positive")
    if any(
        cost.requests <= 0
        or cost.tokens <= 0
        or cost.inner_model_calls <= 0
        or cost.inner_model_calls > policy.limits.inner_model_call_limit
        for cost in costs
    ):
        raise ContractError("Validation endpoint cost exceeds the reviewed envelope")
    percent = policy.limits.reserved_capacity_percent
    reserved_rpm = max(
        policy.limits.minimum_rpm_headroom,
        math.ceil(measurement.rpm * percent / 100),
    )
    reserved_tpm = max(
        policy.limits.minimum_tpm_headroom,
        math.ceil(measurement.tpm * percent / 100),
    )
    available_rpm = measurement.rpm - reserved_rpm
    available_tpm = measurement.tpm - reserved_tpm
    max_request_cost = max(cost.requests for cost in costs)
    max_token_cost = max(cost.tokens for cost in costs)
    if available_rpm < max_request_cost or available_tpm < max_token_cost:
        raise ContractError(
            "Measured capacity cannot preserve reviewed account headroom"
        )
    endpoint_concurrency = min(
        policy.limits.provisioning_concurrency,
        available_rpm // max_request_cost,
        available_tpm // max_token_cost,
    )
    if endpoint_concurrency < 1:
        raise ContractError("Validation endpoint concurrency cannot be scheduled")
    values = {
        "measured_rpm": measurement.rpm,
        "measured_tpm": measurement.tpm,
        "measured_at": measurement.measured_at,
        "reserved_percent": percent,
        "reserved_rpm": reserved_rpm,
        "reserved_tpm": reserved_tpm,
        "available_rpm": available_rpm,
        "available_tpm": available_tpm,
        "outer_request_envelope": sum(cost.requests for cost in costs),
        "worst_case_inner_model_calls": max(
            cost.inner_model_calls for cost in costs
        ),
        "worst_case_inner_tokens": max_token_cost,
        "endpoint_concurrency": endpoint_concurrency,
        "provisioning_concurrency": policy.limits.provisioning_concurrency,
        "telemetry_query_concurrency": policy.limits.telemetry_query_concurrency,
        "runtime_attempt_concurrency": policy.limits.runtime_attempt_concurrency,
        "inner_model_call_limit": policy.limits.inner_model_call_limit,
    }
    return CapacityPlan(
        **values,
        plan_digest=content_hash(
            {
                "measurement": asdict(measurement),
                "plan": values,
            }
        ),
    )


def validate_capacity_plan(
    plan: CapacityPlan,
    *,
    policy: ValidationPolicy,
) -> None:
    values = asdict(plan)
    digest = values.pop("plan_digest")
    expected = content_hash(
        {
            "measurement": {
                "rpm": plan.measured_rpm,
                "tpm": plan.measured_tpm,
                "measured_at": plan.measured_at,
            },
            "plan": values,
        }
    )
    if digest != expected:
        raise ContractError("Validation capacity plan digest is stale")
    if (
        plan.reserved_percent != policy.limits.reserved_capacity_percent
        or plan.reserved_rpm < policy.limits.minimum_rpm_headroom
        or plan.reserved_tpm < policy.limits.minimum_tpm_headroom
        or plan.provisioning_concurrency
        != policy.limits.provisioning_concurrency
        or plan.telemetry_query_concurrency
        != policy.limits.telemetry_query_concurrency
        or plan.runtime_attempt_concurrency
        != policy.limits.runtime_attempt_concurrency
        or plan.inner_model_call_limit != policy.limits.inner_model_call_limit
        or plan.endpoint_concurrency > plan.provisioning_concurrency
    ):
        raise ContractError("Validation capacity plan violates reviewed limits")


class WeightedTokenBucket:
    def __init__(
        self,
        *,
        request_capacity: int,
        token_capacity: int,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_capacity <= 0 or token_capacity <= 0:
            raise ContractError("Token bucket capacity must be positive")
        self._request_capacity = float(request_capacity)
        self._token_capacity = float(token_capacity)
        self._requests = float(request_capacity)
        self._tokens = float(token_capacity)
        self._request_refill = request_capacity / 60.0
        self._token_refill = token_capacity / 60.0
        self._clock = clock
        self._sleeper = sleeper
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self, cost: EndpointCost) -> None:
        if (
            cost.requests <= 0
            or cost.tokens <= 0
            or cost.requests > self._request_capacity
            or cost.tokens > self._token_capacity
        ):
            raise ContractError("Endpoint cost cannot fit in the shared token bucket")
        while True:
            with self._lock:
                self._refill()
                if (
                    self._requests >= cost.requests
                    and self._tokens >= cost.tokens
                ):
                    self._requests -= cost.requests
                    self._tokens -= cost.tokens
                    return
                request_wait = max(
                    0.0,
                    (cost.requests - self._requests) / self._request_refill,
                )
                token_wait = max(
                    0.0,
                    (cost.tokens - self._tokens) / self._token_refill,
                )
                delay = max(request_wait, token_wait, 0.001)
            self._sleeper(delay)

    def reduce_from_rate_limit(
        self,
        *,
        remaining_requests: int | None,
        remaining_tokens: int | None,
        retry_after_seconds: float | None,
    ) -> None:
        with self._lock:
            self._refill()
            if remaining_requests is not None:
                self._requests = min(
                    self._requests,
                    float(max(0, remaining_requests)),
                )
            if remaining_tokens is not None:
                self._tokens = min(
                    self._tokens,
                    float(max(0, remaining_tokens)),
                )
        if retry_after_seconds is not None:
            if not math.isfinite(retry_after_seconds) or retry_after_seconds < 0:
                raise ContractError("Retry-After value is invalid")
            if retry_after_seconds:
                self._sleeper(retry_after_seconds)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._requests = min(
            self._request_capacity,
            self._requests + elapsed * self._request_refill,
        )
        self._tokens = min(
            self._token_capacity,
            self._tokens + elapsed * self._token_refill,
        )


class ValidationScheduler:
    def __init__(self, plan: CapacityPlan, bucket: WeightedTokenBucket) -> None:
        self._plan = plan
        self._bucket = bucket
        self._endpoint_slots = threading.BoundedSemaphore(
            plan.endpoint_concurrency
        )
        self._runtime_locks: dict[str, threading.Lock] = {}
        self._runtime_locks_guard = threading.Lock()

    @contextmanager
    def attempt(self, runtime_id: str, cost: EndpointCost) -> Iterator[None]:
        if cost.inner_model_calls > self._plan.inner_model_call_limit:
            raise ContractError("Attempt exceeds the reviewed inner model-call limit")
        if not runtime_id:
            raise ContractError("Runtime identity is required for scheduling")
        runtime_lock = self._runtime_lock(runtime_id)
        with self._endpoint_slots:
            with runtime_lock:
                self._bucket.acquire(cost)
                yield

    def _runtime_lock(self, runtime_id: str) -> threading.Lock:
        with self._runtime_locks_guard:
            return self._runtime_locks.setdefault(runtime_id, threading.Lock())

    def observe_rate_limit(
        self,
        feedback: dict[str, int | float | None],
    ) -> None:
        requests = feedback.get("remaining_requests")
        tokens = feedback.get("remaining_tokens")
        retry_after = feedback.get("retry_after_seconds")
        self._bucket.reduce_from_rate_limit(
            remaining_requests=(
                int(requests) if isinstance(requests, int) else None
            ),
            remaining_tokens=int(tokens) if isinstance(tokens, int) else None,
            retry_after_seconds=(
                float(retry_after)
                if isinstance(retry_after, (int, float))
                and not isinstance(retry_after, bool)
                else None
            ),
        )
