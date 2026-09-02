from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from agent_insights_quality.util import content_hash


def verification_assignment(
    prepared: Mapping[str, Any],
    authority_id: str,
) -> dict[str, str]:
    value = {
        "authority_id": authority_id,
        "quota_plan_digest": prepared["digests"]["quota_plan_digest"],
        "verifier_commit_sha": prepared["commit_sha"],
        "verifier_digest": prepared["digests"]["shared_validation_digest"],
        "assignment_digest": "",
    }
    payload = copy.deepcopy(value)
    payload.pop("assignment_digest")
    value["assignment_digest"] = content_hash(payload)
    return value
