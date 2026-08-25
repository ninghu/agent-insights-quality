from __future__ import annotations

import re

from agent_insights_quality.util import ROOT, read_yaml


def test_sensitive_key_pattern_matches_quoted_json() -> None:
    policy = read_yaml(ROOT / "config" / "security.yaml")
    patterns = [re.compile(value) for value in policy["forbidden_tracked_patterns"]]
    sample = '"client_' + 'secret": "synthetic"'
    assert any(pattern.search(sample) for pattern in patterns)
