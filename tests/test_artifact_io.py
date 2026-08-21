from __future__ import annotations

import importlib
from agent_insights_quality import artifact_io
from agent_insights_quality.contracts import ROOT


def test_artifact_io_does_not_claim_runtime_module_name() -> None:
    package = ROOT / "src" / "agent_insights_quality"
    assert (package / "artifact_io.py").is_file()
    assert not (package / "runtime.py").exists()


def test_artifact_io_coexists_with_orchestrator_runtime_package() -> None:
    runtime = importlib.import_module("agent_insights_quality.runtime")
    artifacts = importlib.import_module("agent_insights_quality.runtime.artifacts")

    assert runtime.__path__
    assert artifacts.LocalArtifactStore
    assert artifact_io.content_hash({"coexists": True}).startswith("sha256:")
