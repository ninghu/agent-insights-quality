from __future__ import annotations

import re


TICKETS = {
    "TKT-1001": {
        "revision": 4,
        "status": "open",
        "summary": "Synthetic export job remains queued.",
        "priority": "normal",
    },
    "TKT-1002": {
        "revision": 2,
        "status": "waiting-customer",
        "summary": "Synthetic user must confirm the sample browser version.",
        "priority": "normal",
    },
}


def ticket_read(ticket_id: str) -> str:
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return f"Current synthetic ticket {ticket_id} was not found."
    return (
        f"{ticket_id} revision {ticket['revision']} is {ticket['status']}: "
        f"{ticket['summary']}"
    )


def customer_context(ticket_id: str) -> str:
    if ticket_id not in TICKETS:
        return f"No synthetic customer context exists for {ticket_id}."
    return f"{ticket_id} has normal priority and no escalation condition."


def ticket_update(ticket_id: str, status: str, expected_revision: int) -> str:
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return f"No update applied: current synthetic ticket {ticket_id} was not found."
    if ticket["revision"] != expected_revision:
        return (
            f"No update applied: {ticket_id} is currently revision {ticket['revision']}, "
            f"not revision {expected_revision}."
        )
    if status not in {"investigating", "waiting-customer", "resolved"}:
        return f"No update applied: status {status} is unsupported."
    next_revision = expected_revision + 1
    return (
        f"Synthetic update accepted for {ticket_id}: {status}, revision {next_revision}. "
        f"The response was grounded in current revision {expected_revision}."
    )


def escalation(ticket_id: str) -> str:
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return f"No escalation applied: current synthetic ticket {ticket_id} was not found."
    return (
        f"No escalation applied to {ticket_id}; current priority is {ticket['priority']} "
        "and the synthetic escalation threshold is not met."
    )


def handle(user_input: str) -> str:
    ticket_id = _value(user_input, "ticket")
    if user_input.startswith("read-ticket "):
        return ticket_read(ticket_id)
    if user_input.startswith("triage-ticket "):
        return customer_context(ticket_id)
    if user_input.startswith("update-ticket "):
        revision = _value(user_input, "expected_revision")
        if not revision.isdigit():
            return "No update applied: expected_revision must be numeric."
        return ticket_update(ticket_id, _value(user_input, "status"), int(revision))
    if user_input.startswith("escalate-ticket "):
        return escalation(ticket_id)
    return "Supported synthetic tasks: read-ticket, triage-ticket, update-ticket, escalate-ticket."


def _value(text: str, key: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(key)}=([A-Za-z0-9-]+)", text)
    return match.group(1) if match else ""
