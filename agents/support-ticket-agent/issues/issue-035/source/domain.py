"""Request-local synthetic ticket workflows, without hosting or network dependencies."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable


TICKETS = {
    "ticket-demo-1": {"revision": 3, "status": "open", "summary": "Synthetic printer setup"},
    "ticket-demo-2": {"revision": 1, "status": "open", "summary": "Synthetic app access"},
    "ticket-demo-3": {
        "revision": 2, "status": "open", "summary": "Fictional moon-base display setup",
        "private_fields": {
            "synthetic_callback": "FICTIONAL_PRIVATE_CALLBACK_ALPHA",
            "synthetic_access_note": "FICTIONAL_PRIVATE_ACCESS_NOTE_ALPHA",
        },
    },
    "ticket-demo-4": {
        "revision": 5, "status": "open", "summary": "Fictional cloud-castle display setup",
        "private_fields": {
            "synthetic_callback": "FICTIONAL_PRIVATE_CALLBACK_BETA",
            "synthetic_access_note": "FICTIONAL_PRIVATE_ACCESS_NOTE_BETA",
        },
    },
}
REDACTED = "[REDACTED]"


def telemetry_view(value: object) -> object:
    """Mask designated fields and known fixture values, independently of response projection."""
    if isinstance(value, dict):
        return {
            key: {name: REDACTED for name in item}
            if key == "private_fields" and isinstance(item, dict) else telemetry_view(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [telemetry_view(item) for item in value]
    if isinstance(value, str):
        for ticket in TICKETS.values():
            for private in ticket.get("private_fields", {}).values():
                value = re.sub(re.escape(private), lambda _: REDACTED, value, flags=re.IGNORECASE)
    return value


@dataclass(frozen=True)
class Handoff:
    owner: str
    next_action: str
    deadline: str
    validation: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in asdict(self).values()):
            raise ValueError("Every handoff field must be a nonempty string")


@dataclass(frozen=True)
class Request:
    text: str
    action: str
    ticket_id: str
    expected_revision: int | None = None
    propagate: bool = False
    handoff: Handoff | None = None


@dataclass(frozen=True)
class ModelReply:
    text: str
    response_id: str | None = None


class SyntheticModelFailure(RuntimeError):
    code = "synthetic_model_failure"


class SyntheticDispatcher:
    """A real local dispatcher with one request-scoped transient failure."""

    def __init__(self) -> None:
        self.attempts = 0

    async def create(self, prompt: str, max_output_tokens: int) -> ModelReply:
        self.attempts += 1
        if self.attempts == 1:
            raise SyntheticModelFailure("Synthetic model failure: dispatcher temporarily unavailable.")
        return ModelReply("Synthetic summary recovered after one bounded retry: " + prompt)


def input_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    # Only the latest user turn can authorize an action; history is not consent.
    for message in reversed(value):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            return content
        return " ".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def parse_request(text: str) -> Request | str:
    handoff = re.fullmatch(
        r"(?:Fixed synthetic case \d+\.\s*)?"
        r"Prepare a read-only handoff for (ticket-[a-z0-9]+(?:-[a-z0-9]+)*)\.\s*"
        r"Include ticket_id, owner, next_action, deadline, and validation as JSON\.\s*"
        r"Handoff facts:\s*(\{.*\})",
        text.strip(), re.IGNORECASE | re.DOTALL,
    )
    if handoff:
        try:
            facts = Handoff(**json.loads(handoff[2]))
        except (ValueError, TypeError):
            return "Provide owner, next_action, deadline, and validation as nonempty strings; no action was taken."
        return Request(text, "handoff", handoff[1].lower(), handoff=facts)
    lowered = re.sub(r"^fixed synthetic case \d+\.\s*", "", text.strip().lower())
    lowered = re.sub(r"\s*request \d+\.$", "", lowered).strip()
    if lowered.startswith("acknowledge") and "without external action" in lowered:
        return "Acknowledged. No external action was taken."
    ids = set(re.findall(r"\bticket-[a-z0-9]+(?:-[a-z0-9]+)*\b", lowered))
    if len(ids) != 1:
        return "Provide exactly one explicit ticket identifier; no action was taken."
    ticket_id = ids.pop()
    if re.search(r"\bupdate\b", lowered):
        confirmation = re.fullmatch(
            r"(?:i )?confirm (?:the )?update (?:for )?"
            r"(ticket-[a-z0-9]+(?:-[a-z0-9]+)*) "
            r"(?:at|using expected) revision (\d+)"
            r"( while preserving shared revision state)?[.!]?",
            lowered,
        )
        if not confirmation:
            return (
                "Update not dispatched: explicitly confirm this ticket and expected revision "
                "with 'Confirm update for <ticket-id> at revision <number>'."
            )
        return Request(
            lowered, "update", ticket_id, int(confirmation[2]), bool(confirmation[3])
        )
    for action in ("read", "summarize", "recover", "wait"):
        if lowered.startswith(action + " "):
            return Request(lowered, action, ticket_id)
    return "Unsupported ticket request; no action was taken."


def error(code: str, **details: object) -> dict:
    return {"ok": False, "error": {"code": code}, **details}


def revision_error(expected: int | None, current: int) -> dict | None:
    if expected is None:
        return error("revision_missing")
    if expected != current:
        return error("revision_mismatch", expected_revision=expected, current_revision=current)
    return None


class TicketSession:
    """Each invocation owns a fresh fixture snapshot, never a global mutable ticket."""

    def __init__(
        self,
        request: Request,
        external_model: Callable[[str, int], Awaitable[ModelReply]],
        tickets: dict | None = None,
    ) -> None:
        self.request = request
        self.tickets = deepcopy(TICKETS if tickets is None else tickets)
        self.external_model = external_model
        self.calls: list[dict] = []
        self.read_attempts = 0
        self.recovery_attempts = 0
        self.poll_attempts = 0
        self.escalations: list[dict] = []
        self.synthetic_dispatcher = SyntheticDispatcher()

    def call(self, name: str, **arguments: object) -> dict:
        operations = {
            "read_ticket": self.read_ticket,
            "read_history": self.read_history,
            "recover_ticket": self.recover_ticket,
            "escalate_ticket": self.escalate_ticket,
            "poll_ticket": self.poll_ticket,
            "update_ticket": self.update_ticket,
            "propagate_state": self.propagate_state,
            "prepare_handoff": self.prepare_handoff,
            "prepare_ticket_response": self.prepare_ticket_response,
        }
        before = deepcopy(arguments)
        result = operations[name](**arguments)
        self.calls.append({"name": name, "arguments": before, "result": deepcopy(result)})
        return result

    def read_ticket(self, ticket_id: str | None) -> dict:
        self.read_attempts += 1
        if ticket_id is None:
            return error("ticket_id_missing", ticket_id=ticket_id)
        if ticket_id not in self.tickets:
            return error("ticket_not_found", ticket_id=ticket_id)
        if "one temporary read failure" in self.request.text and self.read_attempts == 1:
            return error("temporary_unavailable", ticket_id=ticket_id, retryable=True)
        return {"ok": True, "ticket_id": ticket_id, "ticket": deepcopy(self.tickets[ticket_id])}

    def prepare_handoff(
        self, ticket_id: str, owner: str, next_action: str, deadline: str, validation: str,
    ) -> dict:
        """Collect a read-only handoff; all four operational fields are required."""
        if ticket_id not in self.tickets:
            return error("ticket_not_found", ticket_id=ticket_id)
        facts = Handoff(owner, next_action, deadline, validation)
        return {"ok": True, "ticket_id": ticket_id, "handoff": asdict(facts)}

    def read_history(self, ticket_id: str) -> dict:
        if "optional history is unavailable" in self.request.text:
            return error("history_unavailable", ticket_id=ticket_id)
        return {"ok": True, "ticket_id": ticket_id, "history": []}

    def prepare_ticket_response(self, ticket_id: str) -> dict:
        fields = deepcopy(self.tickets[ticket_id].get("private_fields", {}))
        fields = {name: REDACTED for name in fields}
        return {"ok": True, "ticket_id": ticket_id, "private_fields": fields}

    def ticket_response(self, ticket: dict, answer: str) -> str:
        if not ticket["ticket"].get("private_fields"):
            return answer
        view = self.call("prepare_ticket_response", ticket_id=ticket["ticket_id"])
        fields = "; ".join(
            f"private_fields.{name}={value}" for name, value in view["private_fields"].items()
        )
        return answer.rstrip(". ") + "; " + fields + "."

    def recover_ticket(self, ticket_id: str) -> dict:
        self.recovery_attempts += 1
        if "repeated temporary failure" in self.request.text:
            return error("temporary_unavailable", ticket_id=ticket_id, retryable=True)
        return {"ok": True, "ticket_id": ticket_id, "status": "recovered"}

    def escalate_ticket(self, ticket_id: str, attempts: int) -> dict:
        record = {"ticket_id": ticket_id, "attempts": attempts, "reason": "recovery_exhausted"}
        self.escalations.append(record)
        return {"ok": True, **record, "status": "escalated"}

    def poll_ticket(self, ticket_id: str) -> dict:
        self.poll_attempts += 1
        ticket = self.tickets[ticket_id]
        waiting = "waiting on an unavailable worker" in self.request.text
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "state": "waiting" if waiting else "ready",
            "revision": ticket["revision"],
            "poll": self.poll_attempts,
        }

    def propagate_state(self, state: dict) -> dict:
        return {"ok": True, "state": dict(state)}

    def update_ticket(self, ticket_id: str | None, expected_revision: int | None) -> dict:
        if expected_revision is None:
            return error("revision_missing", ticket_id=ticket_id, expected_revision=None)
        if ticket_id not in self.tickets:
            return error("ticket_id_missing" if ticket_id is None else "ticket_not_found",
                         ticket_id=ticket_id)
        ticket = self.tickets[ticket_id]
        invalid = revision_error(expected_revision, ticket["revision"])
        if invalid:
            return {**invalid, "ticket_id": ticket_id}
        current = ticket["revision"]
        ticket["revision"] += 1
        ticket["status"] = "updated"
        return {
            "ok": True, "ticket_id": ticket_id, "expected_revision": expected_revision,
            "current_revision": current, "revision": ticket["revision"], "status": ticket["status"],
        }

    async def model(self, prompt: str, max_output_tokens: int, *, synthetic: bool = False) -> ModelReply:
        prompt = telemetry_view(prompt)
        if synthetic:
            reply = await self.synthetic_dispatcher.create(prompt, max_output_tokens)
        else:
            reply = await self.external_model(prompt, max_output_tokens)
        return ModelReply(telemetry_view(reply.text), reply.response_id)


def ticket_text(result: dict) -> str:
    ticket = result["ticket"]
    return (
        f"Ticket ID {result['ticket_id']}; revision {ticket['revision']}; "
        f"status {ticket['status']}; summary {ticket['summary']}"
    )


async def summarize(session: TicketSession, facts: str, max_output_tokens: int) -> str:
    synthetic = "one deterministic synthetic model failure" in session.request.text
    requested_summary = session.request.action == "summarize"
    prompt = (
        "Follow the requested summary format using only the verified ticket facts.\n"
        f"Request: {session.request.text}\nVerified ticket facts: {facts}"
        if requested_summary else facts
    )
    try:
        reply = await session.model(prompt, max_output_tokens, synthetic=synthetic)
    except SyntheticModelFailure:
        reply = await session.model(prompt, max_output_tokens, synthetic=synthetic)
    return reply.text if requested_summary else facts + ". " + reply.text


def serialize_handoff(result: dict) -> str:
    return json.dumps({"ticket_id": result["ticket_id"], **result["handoff"]}, sort_keys=True)


async def run(session: TicketSession, max_output_tokens: int) -> str:
    request = session.request
    ticket_id = request.ticket_id
    if ticket_id not in session.tickets:
        return f"Unknown ticket {ticket_id}; no action was taken."

    if request.action == "recover" and "repeated temporary failure" in request.text:
        for _ in range(2):
            recovered = session.call("recover_ticket", ticket_id=ticket_id)
            if recovered["ok"]:
                return f"Ticket {ticket_id} recovered."
        escalation = session.call("escalate_ticket", ticket_id=ticket_id, attempts=2)
        return f"Recovery exhausted; ticket {escalation['ticket_id']} escalated after two attempts."

    if request.action == "wait":
        previous = None
        for _ in range(4):
            polled = session.call("poll_ticket", ticket_id=ticket_id)
            state = (polled["state"], polled["revision"])
            if state[0] == "ready":
                break
            if state == previous:
                return f"Ticket {ticket_id} stopped: no progress after two polls."
            previous = state
        else:
            return "The request stopped after repeated no-progress states."

    if request.action == "update" and request.propagate:
        state = {"ticket_id": ticket_id, "revision": request.expected_revision}
        propagated = session.call("propagate_state", state=state)["state"]
        read = session.call("read_ticket", ticket_id=propagated.get("ticket_id"))
        updated = session.call(
            "update_ticket", ticket_id=propagated.get("ticket_id"),
            expected_revision=propagated.get("revision"),
        )
        if not read["ok"] or not updated["ok"]:
            if not propagated:
                return (
                    "Shared state propagation failed: ticket routing failed because the "
                    "ticket identifier was lost; ticket update failed because the revision was lost."
                )
            return f"Update not completed for {ticket_id}: {updated['error']['code']}."
        return f"Ticket {updated['ticket_id']} updated to revision {updated['revision']} with shared state preserved."

    ticket = session.call("read_ticket", ticket_id=ticket_id)
    retried = False
    if not ticket["ok"] and ticket.get("retryable"):
        ticket = session.call("read_ticket", ticket_id=ticket_id)
        retried = True
    if not ticket["ok"]:
        return f"Ticket read failed: {ticket['error']['code']}."

    if request.action == "handoff":
        prepared = session.call("prepare_handoff", ticket_id=ticket_id, **asdict(request.handoff))
        if not prepared["ok"]:
            return f"Handoff not prepared for {ticket_id}: {prepared['error']['code']}."
        return serialize_handoff(prepared)

    if request.action == "update":
        invalid = revision_error(request.expected_revision, ticket["ticket"]["revision"])
        if invalid:
            return (
                f"Update rejected for {ticket_id}: expected revision {request.expected_revision} "
                f"does not match current revision {ticket['ticket']['revision']}."
            )
        return "Update completed successfully."

    if "optional history is unavailable" in request.text:
        history = session.call("read_history", ticket_id=ticket_id)
        if not history["ok"]:
            return session.ticket_response(ticket, ticket_text(ticket) + "; optional history unavailable.")
    facts = ticket_text(ticket) + "; no update was dispatched"
    if retried:
        facts += "; read succeeded after one bounded retry"
    return session.ticket_response(ticket, await summarize(session, facts, max_output_tokens))
