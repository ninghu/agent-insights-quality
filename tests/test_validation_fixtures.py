from __future__ import annotations

from agent_insights_quality.util import ROOT


def test_superseded_revision_named_validation_fixture_is_removed() -> None:
    root = ROOT / "tests" / "fixtures" / "test_agent_validation"
    assert not (root / "r01").exists()


def test_local_validation_fixture_root_has_no_obsolete_json() -> None:
    root = ROOT / "tests" / "fixtures" / "test_agent_validation"
    assert not list(root.rglob("*.json"))
