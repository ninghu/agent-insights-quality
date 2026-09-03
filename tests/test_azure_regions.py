from __future__ import annotations

import pytest

from agent_insights_quality.azure_regions import (
    location_display_name,
    regions_match,
)
from agent_insights_quality.util import ContractError


def test_location_display_resolution_is_generic() -> None:
    metadata = [
        {"name": "westus2", "displayName": "West US 2"},
        {"name": "uksouth", "displayName": "UK South"},
        {"name": "japaneast", "displayName": "Japan East"},
    ]
    assert location_display_name("westus2", metadata) == "WestUS2"
    assert location_display_name("uk-south", metadata) == "UKSouth"
    assert location_display_name("Japan_East", metadata) == "JapanEast"


def test_location_display_resolution_fails_closed() -> None:
    with pytest.raises(ContractError, match="location is missing"):
        location_display_name("", [])
    with pytest.raises(ContractError, match="resolve uniquely"):
        location_display_name(
            "westus2",
            [
                {"name": "westus2", "displayName": "West US 2"},
                {"name": "west-us-2", "displayName": "Duplicate"},
            ],
        )


def test_region_cross_check_is_normalized_but_not_a_source() -> None:
    assert regions_match("WestUS2", "west-us-2")
    assert not regions_match("", "westus2")
    assert not regions_match("WestUS2", "EastUS")
