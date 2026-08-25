from __future__ import annotations

from agent_insights_quality.util import ROOT, read_yaml


def test_qualification_profiles_forbid_debug_traffic() -> None:
    policy = read_yaml(ROOT / "config" / "automation.yaml")
    assert policy["qualification_profiles_allow_debug_traffic"] is False
    assert policy["clean_window_preflight_required"] is True
