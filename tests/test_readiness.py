from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.cli import main
from agent_insights_quality.contracts import ContractError, ROOT, load_data
from agent_insights_quality.readiness import (
    MANDATORY_RUNTIME_COMPONENTS,
    require_daily_runtime,
    validate_runtime_readiness,
)


def test_runtime_readiness_is_explicitly_disabled() -> None:
    readiness = load_data(ROOT / "config" / "runtime-readiness.yaml")
    validate_runtime_readiness(readiness)
    assert readiness["daily_workflow_enabled"] is False
    assert set(readiness["mandatory_components"]) == MANDATORY_RUNTIME_COMPONENTS
    assert not any(readiness["mandatory_components"].values())


def test_daily_runtime_fails_closed_as_inconclusive(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-runtime-readiness"]) == 1
    output = capsys.readouterr()
    assert "INCONCLUSIVE" in output.err
    assert "Complete and human-review" in output.err
    assert main(["run-daily"]) == 1
    assert "INCONCLUSIVE" in capsys.readouterr().err


def test_readiness_cannot_enable_daily_workflow_early() -> None:
    readiness = deepcopy(load_data(ROOT / "config" / "runtime-readiness.yaml"))
    readiness["daily_workflow_enabled"] = True
    with pytest.raises(ContractError, match="aggregate readiness"):
        validate_runtime_readiness(readiness)
    with pytest.raises(ContractError, match="INCONCLUSIVE"):
        require_daily_runtime(load_data(ROOT / "config" / "runtime-readiness.yaml"))
