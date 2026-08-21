from __future__ import annotations

import importlib.util

import agent_insights_quality.agent_runtime as agent_runtime


def test_agent_runtime_does_not_claim_orchestrator_runtime_package_name() -> None:
    assert agent_runtime.FoundryDeploymentClient
    assert agent_runtime.FoundryInvocationClient
    orchestrator = importlib.util.find_spec("agent_insights_quality.runtime")
    assert orchestrator is None or orchestrator.submodule_search_locations is not None
