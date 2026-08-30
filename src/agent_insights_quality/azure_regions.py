from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from agent_insights_quality.util import ContractError


def normalized_region_key(value: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "")).casefold()


def location_display_name(
    live_location: str,
    metadata: Sequence[Mapping[str, Any]],
) -> str:
    live_key = normalized_region_key(live_location)
    if not live_key:
        raise ContractError("Live Foundry Project location is missing")
    matches = []
    for item in metadata:
        name = normalized_region_key(item.get("name"))
        display_name = str(item.get("displayName") or "").strip()
        if name == live_key and display_name:
            matches.append(display_name)
    if len(matches) != 1:
        raise ContractError(
            "Live Foundry Project location did not resolve uniquely through "
            "Azure location metadata"
        )
    canonical = re.sub(r"\s+", "", matches[0])
    if re.fullmatch(r"[A-Z][A-Za-z]*[0-9]*", canonical) is None:
        raise ContractError("Azure location display name is invalid")
    return canonical


def regions_match(left: Any, right: Any) -> bool:
    return bool(normalized_region_key(left)) and (
        normalized_region_key(left) == normalized_region_key(right)
    )
