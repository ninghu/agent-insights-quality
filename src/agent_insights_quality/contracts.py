from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_insights_quality.results import UnitId

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Target:
    unit_id: UnitId
    agent_type: str
    validation_mode: str
    version_root: Path
    baseline_root: Path
    expectation: Mapping[str, Any]

    @property
    def key(self) -> str:
        return f"{self.unit_id.agent}/{self.unit_id.logical_version}"

    @property
    def is_baseline(self) -> bool:
        return self.unit_id.logical_version == "v0"

    @property
    def is_prompt(self) -> bool:
        return self.agent_type == "prompt"

    def runtime_name(self, profile: str) -> str:
        if profile == "daily":
            return self.unit_id.agent
        if profile == "staging":
            return f"{self.unit_id.agent}-{self.unit_id.logical_version}"
        raise ValueError("Unknown qualification profile")


@dataclass(frozen=True)
class Step:
    step_id: str
    phase: str
    body: Mapping[str, Any]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class Attempt:
    index: int
    steps: tuple[Step, ...]
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Environment:
    profile: str
    account_name: str
    project_name: str
    project_endpoint: str
    application_insights_resource_id: str
    storage_account_name: str
    registry_name: str
    location: str
    region_display: str
    insights_endpoint: str = ""


@dataclass(frozen=True)
class Deployment:
    target_key: str
    agent_name: str
    provider_version: str
    agent_type: str
    source_revision: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Invocation:
    request_id: str
    response_id: str | None
    session_id: str | None
    started_at: str
    completed_at: str
    status: str
    response: Mapping[str, Any] | None = None
    http_status: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class QueryResult:
    records: tuple[JsonObject, ...]
    complete: bool
    error_code: str | None = None


class CloudPort(Protocol):
    environment: Environment

    async def ensure_deployment(
        self,
        target: Target,
        source_revision: str,
        existing: Deployment | None,
        persist: Callable[[Deployment], None],
    ) -> Deployment: ...

    async def activate(self, deployment: Deployment) -> None: ...

    async def create_session(
        self,
        deployment: Deployment,
        request_id: str,
        persist: Callable[[str], None],
    ) -> str: ...

    async def invoke(
        self,
        deployment: Deployment,
        step: Step,
        *,
        request_id: str,
        session_id: str | None,
        previous_response_id: str | None,
        persist: Callable[[Invocation], None],
    ) -> Invocation: ...

    async def query(self, query: str, *, start: str, end: str) -> QueryResult: ...

    async def ensure_monitor(self, agent_name: str) -> str: ...

    async def reset_monitor(self, monitor_id: str) -> None: ...

    async def start_insights(
        self,
        monitor_id: str,
        lookback_hours: float,
        operation_id: str,
        persist: Callable[[JsonObject], None],
    ) -> JsonObject: ...

    async def get_insights_run(self, monitor_id: str, run_id: str) -> JsonObject: ...

    async def list_insights(self, monitor_id: str) -> tuple[JsonObject, ...]: ...


class SolPort(Protocol):
    async def complete_json(
        self,
        *,
        instructions: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> JsonObject: ...
