from __future__ import annotations

from typing import Any

from agent_insights_quality.contracts import ContractError


MANDATORY_RUNTIME_COMPONENTS = {
    "infrastructure",
    "healthy_agents",
    "scenario_catalog",
    "production_orchestrator",
    "deterministic_scoring",
    "copilot_judging",
    "quality_memory",
    "ado_synchronization",
    "reporting_and_email",
    "live_qualification",
}


def validate_runtime_readiness(data: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "status",
        "daily_workflow_enabled",
        "mandatory_components",
    }
    if set(data) != expected_keys:
        raise ContractError("config/runtime-readiness.yaml: unexpected or missing fields")
    if data["schema_version"] != "1.0.0":
        raise ContractError("config/runtime-readiness.yaml: unsupported schema_version")
    components = data["mandatory_components"]
    if not isinstance(components, dict) or set(components) != MANDATORY_RUNTIME_COMPONENTS:
        raise ContractError("config/runtime-readiness.yaml: mandatory component set changed")
    if not all(isinstance(value, bool) for value in components.values()):
        raise ContractError("config/runtime-readiness.yaml: component readiness must be boolean")
    ready = all(components.values())
    if data["daily_workflow_enabled"] is not ready:
        raise ContractError(
            "config/runtime-readiness.yaml: daily_workflow_enabled must equal aggregate readiness"
        )
    expected_status = "ready" if ready else "contract_scaffolding"
    if data["status"] != expected_status:
        raise ContractError(
            f"config/runtime-readiness.yaml: status must be {expected_status}"
        )


def require_daily_runtime(data: dict[str, Any]) -> None:
    validate_runtime_readiness(data)
    missing = sorted(
        name for name, available in data["mandatory_components"].items() if not available
    )
    if missing:
        raise ContractError(
            "INCONCLUSIVE: daily runtime is not ready. Incomplete components: "
            + ", ".join(missing)
            + ". Complete and human-review these phases before enabling daily automation."
        )
