from __future__ import annotations

import re


RESTRICTED_ACTION = re.compile(r"\bkill\b", re.IGNORECASE)
REFUSAL = (
    "I cannot process this finance request because the content safety guardrail blocked it."
)


def check_request_content(text: str) -> dict:
    """Apply the restricted-action content rule to the current user request."""
    match = RESTRICTED_ACTION.search(text)
    return {
        "decision": "block" if match else "allow",
        "rule": "restricted_action_term",
        "pattern": RESTRICTED_ACTION.pattern,
        "matched_text": match.group() if match else "",
        "match_start": match.start() if match else -1,
        "match_end": match.end() if match else -1,
    }
