from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from agent_insights_quality.errors import QualityError


def normalized_region_key(value: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "")).casefold()


def location_display_name(
    live_location: str,
    metadata: Sequence[Mapping[str, Any]],
) -> str:
    live_key = normalized_region_key(live_location)
    if not live_key:
        raise QualityError("project_location_missing")
    matches = []
    for item in metadata:
        name = normalized_region_key(item.get("name"))
        display_name = str(item.get("displayName") or "").strip()
        if name == live_key and display_name:
            matches.append(display_name)
    if len(matches) != 1:
        raise QualityError("region_metadata_unavailable")
    canonical = re.sub(r"\s+", "", matches[0])
    if re.fullmatch(r"[A-Z][A-Za-z]*[0-9]*", canonical) is None:
        raise QualityError("region_display_invalid")
    return canonical


def regions_match(left: Any, right: Any) -> bool:
    return bool(normalized_region_key(left)) and (
        normalized_region_key(left) == normalized_region_key(right)
    )
