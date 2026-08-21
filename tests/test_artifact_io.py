from __future__ import annotations

import importlib
import sys

import agent_insights_quality

from agent_insights_quality import artifact_io
from agent_insights_quality.contracts import ROOT


def test_artifact_io_does_not_claim_runtime_module_name() -> None:
    package = ROOT / "src" / "agent_insights_quality"
    assert (package / "artifact_io.py").is_file()
    assert not (package / "runtime.py").exists()


def test_artifact_io_coexists_with_orchestrator_runtime_package(
    tmp_path,
    monkeypatch,
) -> None:
    package_extension = tmp_path / "agent_insights_quality"
    runtime_package = package_extension / "runtime"
    runtime_package.mkdir(parents=True)
    (runtime_package / "__init__.py").write_text(
        "ORCHESTRATOR_RUNTIME = True\n",
        encoding="ascii",
    )
    monkeypatch.setattr(
        agent_insights_quality,
        "__path__",
        [*agent_insights_quality.__path__, str(package_extension)],
    )
    sys.modules.pop("agent_insights_quality.runtime", None)
    importlib.invalidate_caches()

    runtime = importlib.import_module("agent_insights_quality.runtime")

    assert runtime.ORCHESTRATOR_RUNTIME is True
    assert artifact_io.content_hash({"coexists": True}).startswith("sha256:")
