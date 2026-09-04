from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest

from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import ContractError


def test_daily_selection_is_stable_unique_and_covers_every_issue_in_two_days() -> None:
    agents, issues = load_catalogs()
    digest = catalog_hashes(agents, issues)["issues"]
    monday = select_daily(date(2026, 8, 24), agents, issues, digest)
    repeated = select_daily(date(2026, 8, 24), agents, issues, digest)
    tuesday = select_daily(date(2026, 8, 25), agents, issues, digest)
    assert monday == repeated
    assert sum(len(selected) for selected in monday.values()) == 20
    assert sum(len(selected) for selected in monday.values()) + len(monday) == 25
    for agent in agents["agents"]:
        name = agent["name"]
        assert len(monday[name]) == 4
        assert len(set(monday[name])) == 4
        assert set(monday[name]) | set(tuesday[name]) == set(agent["issue_ids"])


def test_daily_rotation_balances_each_issue_over_ten_business_days() -> None:
    agents, issues = load_catalogs()
    digest = catalog_hashes(agents, issues)["issues"]
    selections = []
    current = date(2026, 8, 24)
    while len(selections) < 10:
        if current.weekday() < 5:
            selections.append(select_daily(current, agents, issues, digest))
        current += timedelta(days=1)

    for agent in agents["agents"]:
        frequencies = Counter(
            issue_id
            for selected in selections
            for issue_id in selected[agent["name"]]
        )
        assert set(frequencies) == set(agent["issue_ids"])
        assert max(frequencies.values()) - min(frequencies.values()) <= 1


def test_weekends_are_rejected() -> None:
    agents, issues = load_catalogs()
    digest = catalog_hashes(agents, issues)["issues"]
    with pytest.raises(ContractError, match="Monday through Friday"):
        select_daily(date(2026, 8, 22), agents, issues, digest)
