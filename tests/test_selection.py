from __future__ import annotations

from datetime import date

import pytest

from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import ContractError


def test_daily_selection_is_stable_and_covers_every_issue_in_two_days() -> None:
    agents, issues = load_catalogs()
    digest = catalog_hashes(agents, issues)["issues"]
    monday = select_daily(date(2026, 8, 24), agents, issues, digest)
    repeated = select_daily(date(2026, 8, 24), agents, issues, digest)
    tuesday = select_daily(date(2026, 8, 25), agents, issues, digest)
    assert monday == repeated
    for agent in agents["agents"]:
        name = agent["name"]
        assert len(monday[name]) == 5
        assert len(set(monday[name])) == 5
        assert set(monday[name]) | set(tuesday[name]) == set(agent["issue_ids"])


def test_weekends_are_rejected() -> None:
    agents, issues = load_catalogs()
    digest = catalog_hashes(agents, issues)["issues"]
    with pytest.raises(ContractError, match="Monday through Friday"):
        select_daily(date(2026, 8, 22), agents, issues, digest)
