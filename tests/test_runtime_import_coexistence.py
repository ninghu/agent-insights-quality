from __future__ import annotations

import importlib.util

import agent_insights_quality.agent_runtime as agent_runtime
import agent_insights_quality.runtime as orchestrator_runtime
from agent_insights_quality.deployment import FoundryDeploymentClient
from agent_insights_quality.runtime.config import RuntimeConfig
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.traffic import FoundryInvocationClient


def test_agent_runtime_does_not_claim_orchestrator_runtime_package_name() -> None:
    assert agent_runtime.FoundryDeploymentClient is FoundryDeploymentClient
    assert agent_runtime.FoundryInvocationClient is FoundryInvocationClient
    orchestrator = importlib.util.find_spec("agent_insights_quality.runtime")
    assert orchestrator is not None
    assert orchestrator.submodule_search_locations is not None
    assert orchestrator_runtime.RuntimeConfig is RuntimeConfig
    assert orchestrator_runtime.RuntimeFailure is RuntimeFailure
    assert agent_runtime is not orchestrator_runtime
