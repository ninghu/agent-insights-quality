from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_cycle import ValidationCycleController
from agent_insights_quality.validation_provisioning import ProjectDeployment
from agent_insights_quality.validation_runtime import DeployedRuntime


def _runtime_agent_payload(
    planned: Any,
    runtime: DeployedRuntime,
) -> dict[str, Any]:
    return {
        "authority_id": planned.authority_id,
        "canonical_agent": planned.canonical_agent,
        "logical_version": planned.logical_version,
        "runtime_kind": planned.runtime_kind,
        "framework": planned.framework,
        "runtime_agent_name": runtime.runtime_agent_name,
        "runtime_agent_version": runtime.runtime_agent_version,
        "provider_agent_id": runtime.provider_agent_id,
        "provider_agent_version_id": runtime.provider_agent_version_id,
        "provider_content_digest": runtime.provider_content_digest,
        "hosted_identity_id": runtime.hosted_identity_id,
        "hosted_blueprint_id": runtime.hosted_blueprint_id,
        "hosted_deployment_id": runtime.hosted_deployment_id,
        "foundry_agent_name": runtime.runtime_agent_name,
        "foundry_agent_version": runtime.runtime_agent_version,
        "runtime_principal_id": runtime.runtime_principal_id,
        "telemetry_identity_id": runtime.telemetry_identity_id,
        "connection_ids": list(runtime.connection_ids),
    }


def _deployed_runtime(item: Mapping[str, Any]) -> DeployedRuntime:
    return DeployedRuntime(
        authority_id=str(item["authority_id"]),
        runtime_kind=str(item["runtime_kind"]),
        runtime_agent_name=str(item["runtime_agent_name"]),
        runtime_agent_version=str(item["runtime_agent_version"]),
        provider_agent_id=str(item["provider_agent_id"]),
        provider_agent_version_id=str(item["provider_agent_version_id"]),
        provider_content_digest=str(item["provider_content_digest"]),
        hosted_identity_id=item.get("hosted_identity_id"),
        hosted_blueprint_id=item.get("hosted_blueprint_id"),
        hosted_deployment_id=item.get("hosted_deployment_id"),
        runtime_principal_id=item.get("runtime_principal_id"),
        telemetry_identity_id=str(item["telemetry_identity_id"]),
        connection_ids=tuple(item["connection_ids"]),
    )


def _project_from_lifecycle(
    lifecycle: Mapping[str, Any],
) -> ProjectDeployment:
    project = lifecycle["project"]
    if (
        project["state"] != "bound"
        or not project["provider_id"]
        or not project["project_principal_id"]
        or len(lifecycle["runtime_topology"]["connection_ids"]) != 2
    ):
        raise ContractError("Retained validation Project topology is incomplete")
    return ProjectDeployment(
        project_name=str(project["name"]),
        project_id=str(project["provider_id"]),
        project_principal_id=str(project["project_principal_id"]),
        project_endpoint="",
        connection_ids=tuple(lifecycle["runtime_topology"]["connection_ids"]),
        role_assignment_ids=(),
        resource_observations=(),
    )


@contextmanager
def lifecycle_heartbeat(
    controller: ValidationCycleController,
    *,
    now: Callable[[], datetime],
    interval_seconds: int = 45,
) -> Iterator[None]:
    if interval_seconds <= 0 or interval_seconds >= 60:
        raise ContractError("Validation heartbeat interval must be below 60 seconds")
    stopped = threading.Event()
    failures: list[BaseException] = []

    def pulse() -> None:
        while not stopped.wait(interval_seconds):
            try:
                controller.heartbeat(now=now())
            except (ContractError, OSError, RuntimeError) as error:
                failures.append(error)
                return

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=interval_seconds)
    if failures:
        raise ContractError("Validation lifecycle heartbeat failed") from failures[0]
