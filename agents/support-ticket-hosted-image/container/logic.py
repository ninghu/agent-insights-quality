from __future__ import annotations

from typing import Any


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

TOOLS = [
    {
        "type": "function",
        "name": "ticket_read",
        "description": "Read the current revision of a synthetic ticket.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ticket_id"],
            "properties": {
                "ticket_id": {"type": "string", "enum": ["TKT-1001", "TKT-1002"]},
            },
        },
    },
    {
        "type": "function",
        "name": "customer_context",
        "description": "Read bounded synthetic customer context for the current ticket.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ticket_id"],
            "properties": {
                "ticket_id": {"type": "string", "enum": ["TKT-1001", "TKT-1002"]},
            },
        },
    },
    {
        "type": "function",
        "name": "ticket_update",
        "description": "Preview a synthetic ticket update without persisting state.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ticket_id", "status", "expected_revision"],
            "properties": {
                "ticket_id": {"type": "string", "enum": ["TKT-1001", "TKT-1002"]},
                "status": {
                    "type": "string",
                    "enum": ["investigating", "waiting-customer", "resolved"],
                },
                "expected_revision": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "type": "function",
        "name": "escalation",
        "description": "Evaluate bounded escalation against current synthetic ticket state.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ticket_id"],
            "properties": {
                "ticket_id": {"type": "string", "enum": ["TKT-1001", "TKT-1002"]},
            },
        },
    },
]

INSTRUCTIONS = (
    "Route command prefixes exactly: read-ticket -> ticket_read; triage-ticket -> customer_context; "
    "update-ticket -> ticket_update. Call exactly that one tool and return its result verbatim. Stay "
    "grounded in the requested current ticket. ticket_update is explicitly a dry-run preview and "
    "never persists or claims a completed update."
)


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
        return f"No dry-run preview: current synthetic ticket {ticket_id} was not found."
    if ticket["revision"] != expected_revision:
        return (
            f"No dry-run preview: {ticket_id} is currently revision {ticket['revision']}, "
            f"not revision {expected_revision}."
        )
    if status not in {"investigating", "waiting-customer", "resolved"}:
        return f"No dry-run preview: status {status} is unsupported."
    next_revision = expected_revision + 1
    return (
        f"Dry-run update preview for {ticket_id}: {status}, prospective revision "
        f"{next_revision}. No state was persisted; current revision remains {expected_revision}."
    )


def escalation(ticket_id: str) -> str:
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return f"No escalation applied: current synthetic ticket {ticket_id} was not found."
    return (
        f"No escalation applied to {ticket_id}; current priority is {ticket['priority']} "
        "and the synthetic escalation threshold is not met."
    )


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if name == "ticket_read":
        return ticket_read(str(arguments["ticket_id"]))
    if name == "customer_context":
        return customer_context(str(arguments["ticket_id"]))
    if name == "ticket_update":
        return ticket_update(
            str(arguments["ticket_id"]),
            str(arguments["status"]),
            int(arguments["expected_revision"]),
        )
    if name == "escalation":
        return escalation(str(arguments["ticket_id"]))
    raise ValueError(f"Unsupported ticket tool: {name}")
