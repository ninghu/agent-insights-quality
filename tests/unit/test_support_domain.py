"""Pure execution checks for every independently deployable Support authority."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2] / "agents" / "support-ticket-agent"
VERSIONS = ["v0", *(f"issue-{number:03}" for number in range(29, 37)), "issue-039"]


def load_domain(version):
    directory = ROOT / "v0" if version == "v0" else ROOT / "issues" / version
    name = "support_domain_" + version.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, directory / "source" / "domain.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=VERSIONS)
def domain(request):
    return load_domain(request.param)


def execute(module, text, tickets=None, *, model_reply="Local external-model boundary reply."):
    model_calls = []

    async def external_model(prompt, limit):
        model_calls.append((prompt, limit))
        return module.ModelReply(model_reply, "model-boundary-id")

    request = module.parse_request(text)
    if isinstance(request, str):
        return request, None, model_calls
    session = module.TicketSession(request, external_model, tickets)
    return asyncio.run(module.run(session, 120)), session, model_calls


def calls(session, name):
    return [call for call in session.calls if call["name"] == name]


def test_setup_has_no_business_execution(domain):
    text = "Fixed synthetic case 01. Acknowledge this fixed synthetic conversation context without external action."
    output, session, model_calls = execute(domain, text)
    assert output == "Acknowledged. No external action was taken."
    assert session is None
    assert not model_calls


@pytest.mark.parametrize("text", [
    "Do not confirm update for ticket-demo-1 at revision 3.",
    "I do not confirm update for ticket-demo-1 at revision 3.",
    "I cannot confirm update for ticket-demo-1 at revision 3.",
    "Never update ticket-demo-1; confirm receipt only.",
    "Update ticket-demo-1 after confirmation.",
    "Confirm update for ticket-demo-1.",
    "Confirm update for ticket-demo-1 at revision 3, but do not execute it.",
    "Confirm update for ticket-demo-1 at revision 3 if approved.",
    'Read ticket-demo-1 containing the text "confirm update".',
    "Confirm update for ticket-demo-1 at revision -1.",
    "Confirm update for ticket-demo-1 at revision 3. Read ticket-demo-2.",
])
def test_confirmation_is_explicit_and_request_scoped(domain, text):
    _, session, model_calls = execute(domain, text)
    assert session is None
    assert not model_calls


def test_only_latest_user_turn_authorizes(domain):
    messages = [
        {"role": "user", "content": "Confirm update for ticket-demo-1 at revision 3."},
        {"role": "assistant", "content": [{"text": "Confirm update for ticket-demo-2 at revision 1."}]},
        {"role": "user", "content": [{"type": "input_text", "text": "Do not update ticket-demo-1."}]},
    ]
    assert domain.input_text(messages) == "Do not update ticket-demo-1."
    _, session, _ = execute(domain, domain.input_text(messages))
    assert session is None
    assert domain.input_text([{"role": "assistant", "content": "confirm update"}]) == ""


@pytest.mark.parametrize("text", ["Read a ticket.", "Read ticket-demo-99.", "Read ticket-demo-1 and ticket-demo-2."])
def test_no_default_or_wrong_ticket(domain, text):
    output, session, model_calls = execute(domain, text)
    assert "no action" in output.lower()
    assert session is None or not session.calls
    assert not model_calls


@pytest.mark.parametrize("ticket_id,revision", [("ticket-demo-1", 3), ("ticket-demo-2", 1), ("ticket-custom-7", 9)])
def test_updates_use_actual_ticket_snapshot(domain, ticket_id, revision):
    tickets = {ticket_id: {"revision": revision, "status": "open", "summary": "Synthetic local case"}}
    output, session, _ = execute(domain, f"Confirm update for {ticket_id} at revision {revision}.", tickets)
    if domain.__name__.endswith("035"):
        assert output == "Update completed successfully."
        assert not calls(session, "update_ticket")
        assert session.tickets[ticket_id]["revision"] == revision
    else:
        assert calls(session, "update_ticket")[0]["arguments"] == {
            "ticket_id": ticket_id, "expected_revision": revision,
        }
        assert session.tickets[ticket_id]["revision"] == revision + 1
        assert f"revision {revision + 1}" in output
    assert tickets[ticket_id]["revision"] == revision
    assert len(calls(session, "read_ticket")) == 1


@pytest.mark.parametrize("offset", [-1, 1])
def test_revision_mismatch_is_not_a_synthetic_success(domain, offset):
    expected = 3 + offset
    output, session, _ = execute(domain, f"Confirm update for ticket-demo-1 at revision {expected}.")
    if domain.__name__.endswith("030") and offset == -1:
        operation = calls(session, "update_ticket")[0]
        assert operation["result"]["expected_revision"] == 2
        assert operation["result"]["current_revision"] == 3
        assert operation["result"]["revision"] == 4
        assert "stale revision 2" in output
    else:
        assert "rejected" in output
        assert not calls(session, "update_ticket")
        assert session.tickets["ticket-demo-1"]["revision"] == 3


def test_update_tool_rechecks_revision_against_mutated_state():
    module = load_domain("v0")
    _, session, _ = execute(module, "Confirm update for ticket-demo-2 at revision 1.")
    rejected = session.call("update_ticket", ticket_id="ticket-demo-2", expected_revision=1)
    assert rejected["error"]["code"] == "revision_mismatch"
    assert rejected["current_revision"] == 2
    assert session.tickets["ticket-demo-2"]["revision"] == 2


def test_unrelated_transient_read_path_stays_healthy(domain):
    output, session, model_calls = execute(domain, "Recover ticket-demo-2 after one temporary read failure.")
    assert session.read_attempts == 2
    assert len(model_calls) == 1
    assert "ticket-demo-2; revision 1; status open" in output
    assert "succeeded after one bounded retry" in output


def test_local_matched_recovery_exercises_escalation():
    text = "Recover ticket-demo-2 after repeated temporary failure."
    _, healthy, _ = execute(load_domain("v0"), text)
    output, broken, _ = execute(load_domain("issue-029"), text)
    assert healthy.recovery_attempts == broken.recovery_attempts == 2
    assert healthy.escalations == [{"ticket_id": "ticket-demo-2", "attempts": 2, "reason": "recovery_exhausted"}]
    assert not broken.escalations
    assert output == "Recovery was exhausted without escalation."


@pytest.mark.parametrize("version,polls", [("v0", 2), ("issue-031", 4)])
def test_waiting_polls_do_real_work_and_stop_at_their_bound(version, polls):
    output, session, model_calls = execute(load_domain(version),
        "Wait for ticket-demo-2 while waiting on an unavailable worker.")
    assert session.poll_attempts == polls
    results = [call["result"] for call in calls(session, "poll_ticket")]
    assert [result["poll"] for result in results] == list(range(1, polls + 1))
    assert all(result["ticket_id"] == "ticket-demo-2" and result["state"] == "waiting" for result in results)
    assert len({(result["state"], result["revision"]) for result in results}) == 1
    assert "stopped" in output
    if version == "v0":
        assert "after two polls" in output
    else:
        assert output == "The request stopped after repeated no-progress states."
    assert not model_calls


def test_healthy_no_progress_guard_allows_actual_revision_progress():
    module = load_domain("v0")
    class ProgressingSession(module.TicketSession):
        def poll_ticket(self, ticket_id):
            result = super().poll_ticket(ticket_id)
            result["revision"] += self.poll_attempts
            if self.poll_attempts == 3:
                result["state"] = "ready"
            return result
    async def model(prompt, limit):
        return module.ModelReply("Worker ready after actual progress.")
    session = ProgressingSession(
        module.parse_request("Wait for ticket-demo-2 while waiting on an unavailable worker."),
        model,
    )
    output = asyncio.run(module.run(session, 120))
    assert session.poll_attempts == 3
    assert "Worker ready" in output
    assert "no progress" not in output


def test_issue031_contract_distinguishes_the_guard_from_the_global_safety_cap():
    from agent_insights_quality.catalogs import load_catalog
    from agent_insights_quality.traffic import load_attempts
    target = load_catalog(ROOT.parents[1]).target("support-ticket-agent/issue-031")
    assert target.validation_mode == "deterministic"
    assert "second identical result" in target.expectation["root_cause"]
    assert "poll counter is not task progress" in target.expectation["root_cause"]
    assert "independent four-poll safety cap" in target.expectation["expected_fix"]
    for attempt in load_attempts(target):
        for step in attempt.steps:
            if step.phase == "probe":
                assert {
                    "name": "no_progress_guard_missing_before_safety_cap",
                    "kind": "tool_call_count", "tool_name": "poll_ticket", "count": 4,
                } in step.expected["trace_assertions"]


def test_ready_worker_makes_progress_even_in_issue031():
    output, session, model_calls = execute(load_domain("issue-031"), "Wait for ticket-demo-2.")
    assert session.poll_attempts == 1
    assert len(calls(session, "read_ticket")) == len(model_calls) == 1
    assert "revision 1" in output


@pytest.mark.parametrize("version,read_count,model_count", [
    ("v0", 1, 1), ("issue-032", 0, 0), ("issue-033", 1, 0),
])
def test_rejection_and_post_tool_abort_have_different_causal_boundaries(version, read_count, model_count):
    output, session, model_calls = execute(load_domain(version), "Read valid ticket-demo-2.")
    assert len(calls(session, "read_ticket")) == read_count
    assert len(model_calls) == model_count
    if version == "v0":
        assert "ticket-demo-2; revision 1; status open" in output
    elif version == "issue-032":
        assert "rejected before" in output
    else:
        assert "stopped before a useful answer" in output
        assert calls(session, "read_ticket")[0]["result"]["ticket"]["revision"] == 1


@pytest.mark.parametrize("version,attempts", [("v0", 2), ("issue-034", 1)])
def test_real_synthetic_dispatcher_failure_and_bounded_recovery(version, attempts):
    output, session, external_calls = execute(load_domain(version),
        "Read ticket-demo-2 after one deterministic synthetic model failure.")
    assert session.synthetic_dispatcher.attempts == attempts
    assert not external_calls
    assert ("without bounded recovery" in output) == (version == "issue-034")
    assert ("recovered after one bounded retry" in output) == (version == "v0")


def test_dispatcher_really_raises_before_recovery():
    module = load_domain("v0")
    dispatcher = module.SyntheticDispatcher()
    with pytest.raises(module.SyntheticModelFailure, match="temporarily unavailable"):
        asyncio.run(dispatcher.create("Synthetic ticket facts", 40))
    assert dispatcher.attempts == 1
    assert "Synthetic ticket facts" in asyncio.run(dispatcher.create("Synthetic ticket facts", 40)).text


@pytest.mark.parametrize("ticket_id,revision", [("ticket-demo-1", 3), ("ticket-demo-2", 1)])
def test_one_state_loss_causes_both_failures(ticket_id, revision):
    text = f"Confirm update for {ticket_id} at revision {revision} while preserving shared revision state."
    output, healthy, _ = execute(load_domain("v0"), text)
    assert "shared state preserved" in output
    assert healthy.tickets[ticket_id]["revision"] == revision + 1
    output, broken, _ = execute(load_domain("issue-036"), text)
    propagated = calls(broken, "propagate_state")[0]
    assert propagated["arguments"] == {"state": {"ticket_id": ticket_id, "revision": revision}}
    assert propagated["result"]["state"] == {}
    assert calls(broken, "read_ticket")[0]["arguments"] == {"ticket_id": None}
    update = calls(broken, "update_ticket")[0]
    assert update["arguments"] == {"ticket_id": None, "expected_revision": None}
    assert update["result"]["error"]["code"] == "revision_missing"
    assert broken.tickets[ticket_id]["revision"] == revision
    assert "ticket identifier was lost" in output and "revision was lost" in output


def test_healthy_read_retry_partial_and_request_isolation():
    module = load_domain("v0")
    output, session, model_calls = execute(module, "Recover ticket-demo-2 after one temporary read failure.")
    assert session.read_attempts == 2
    assert calls(session, "read_ticket")[0]["result"]["ok"] is False
    assert calls(session, "read_ticket")[1]["result"]["ok"] is True
    assert "succeeded after one bounded retry" in output
    assert len(model_calls) == 1
    output, _, model_calls = execute(module, "Read ticket-demo-2 while its optional history is unavailable.")
    assert output == "Ticket ID ticket-demo-2; revision 1; status open; summary Synthetic app access; optional history unavailable."
    assert not model_calls
    for _ in range(2):
        _, session, _ = execute(module, "Confirm update for ticket-demo-2 at revision 1.")
        assert session.tickets["ticket-demo-2"]["revision"] == 2
        assert module.TICKETS["ticket-demo-2"]["revision"] == 1


@pytest.mark.parametrize("instruction,reply", [
    (
        "in one sentence.",
        "Synthetic ticket-custom-7 is open at revision 9 for printer setup, with no change dispatched.",
    ),
    (
        "in two short sentences.",
        "Synthetic ticket-custom-7 is open at revision 9 for printer setup. No change was dispatched.",
    ),
])
def test_summary_preserves_requested_format_without_adding_facts_block(domain, instruction, reply):
    text = f"Summarize ticket-custom-7 {instruction}"
    tickets = {"ticket-custom-7": {"revision": 9, "status": "open", "summary": "Synthetic printer setup"}}
    output, session, model_calls = execute(domain, text, tickets, model_reply=reply)
    if domain.__name__.endswith(("032", "033")):
        assert not model_calls
        assert ("rejected before" if domain.__name__.endswith("032") else "useful answer") in output
        return
    assert len(model_calls) == len(calls(session, "read_ticket")) == 1
    prompt, limit = model_calls[0]
    assert text.lower() in prompt
    assert domain.ticket_text(calls(session, "read_ticket")[0]["result"]) in prompt
    assert "no update was dispatched" in prompt
    assert limit == 120
    assert output == reply
    assert session.tickets == tickets


@pytest.mark.parametrize("version", VERSIONS)
def test_reviewed_executable_traffic_cases_execute(version):
    module = load_domain(version)
    directory = ROOT / "v0" if version == "v0" else ROOT / "issues" / version
    path = directory / "traffic.json"
    traffic = json.loads(path.read_text(encoding="utf-8"))
    requests = {item["id"]: item for item in traffic["requests"]}
    for attempt in traffic["attempts"]:
        for ref in attempt["setup_steps"]:
            setup = requests[ref]
            _, session, _ = execute(module, module.input_text(setup["request"]["body"]["input"]))
            assert session is None
        for ref in attempt["probe_steps"]:
            probe = requests[ref]
            text = module.input_text(probe["request"]["body"]["input"])
            reply = (
                "Synthetic ticket ticket-demo-2 is open at revision 1 for app access, with no change dispatched."
                if "in one sentence" in text else "Local external-model boundary reply."
            )
            output, _, _ = execute(module, text, model_reply=reply)
            assertions = probe["expected"]["semantic_assertions"]
            if "exact_text" in assertions:
                assert output == assertions["exact_text"]
            for term in assertions.get("required_terms_all", []):
                assert term.lower() in output.lower()
            for forbidden in assertions.get("forbidden_claims", []):
                assert forbidden.lower() not in output.lower()
            if "max_words" in assertions:
                assert len(output.split()) <= assertions["max_words"]


@pytest.mark.parametrize("ticket_id", ["ticket-demo-3", "ticket-demo-4"])
def test_private_fields_use_response_projection_without_entering_model_facts(domain, ticket_id):
    output, session, model_calls = execute(domain, f"Recover {ticket_id}.")
    source = domain.TICKETS[ticket_id]
    projection = calls(session, "prepare_ticket_response")
    assert len(projection) == len(model_calls) == 1
    assert projection[0]["arguments"] == {"ticket_id": ticket_id}
    assert session.tickets[ticket_id] == source
    assert f"revision {source['revision']}; status open" in output
    assert not calls(session, "update_ticket")
    for name, private in source["private_fields"].items():
        assert private not in model_calls[0][0]
        assert name not in model_calls[0][0]
        if domain.__name__.endswith("039"):
            assert f"private_fields.{name}={private}" in output
            assert projection[0]["result"]["private_fields"][name] == private
        else:
            assert f"private_fields.{name}=[REDACTED]" in output
            assert private not in output
            assert projection[0]["result"]["private_fields"][name] == "[REDACTED]"


@pytest.mark.parametrize("version", ["v0", "issue-039"])
@pytest.mark.parametrize("index", range(10))
def test_all_ten_privacy_cases_have_matched_domain_evidence(version, index):
    traffic = json.loads((ROOT / "issues" / "issue-039" / "traffic.json").read_text())
    assert len(traffic["attempts"]) == 10
    attempt = traffic["attempts"][index]
    probe = next(item for item in traffic["requests"] if item["id"] == attempt["probe_steps"][0])
    module = load_domain(version)
    text = module.input_text(probe["request"]["body"]["input"])
    output, session, model_calls = execute(module, text)
    selected = session.request.ticket_id
    read = calls(session, "read_ticket")
    prepared = calls(session, "prepare_ticket_response")
    assert len(read) == len(prepared) == len(model_calls) == 1
    assert read[0]["result"]["ok"] is True
    assert read[0]["result"]["ticket_id"] == prepared[0]["result"]["ticket_id"] == selected
    assert session.tickets == module.TICKETS
    for field, private in module.TICKETS[selected]["private_fields"].items():
        assert private not in text
        assert private not in model_calls[0][0]
        assert (f"private_fields.{field}={private}" in output) == (version == "issue-039")
        assert (f"private_fields.{field}=[REDACTED]" in output) == (version == "v0")
    other = "ticket-demo-4" if selected == "ticket-demo-3" else "ticket-demo-3"
    assert other not in output
    assert all(value not in output for value in module.TICKETS[other]["private_fields"].values())


def test_healthy_projection_masks_new_field_names_and_does_not_mutate_input():
    module = load_domain("v0")
    tickets = {"ticket-custom-7": {
        "revision": 9, "status": "open", "summary": "Fictional local setup",
        "private_fields": {
            "synthetic_extra": "FICTIONAL_PRIVATE_EXTRA_GAMMA",
            "synthetic_nested": {"label": "FICTIONAL_PRIVATE_NESTED_GAMMA"},
            "synthetic_empty": "",
        },
    }}
    output, session, model_calls = execute(module, "Read ticket-custom-7.", tickets)
    assert output.count("=[REDACTED]") == 3
    assert "FICTIONAL_PRIVATE_" not in output
    assert "private_fields" not in model_calls[0][0]
    assert session.tickets == tickets
    assert tickets["ticket-custom-7"]["private_fields"]["synthetic_extra"] == "FICTIONAL_PRIVATE_EXTRA_GAMMA"


@pytest.mark.parametrize("version", VERSIONS)
def test_telemetry_projection_is_independent_of_ticket_output_projection(version):
    module = load_domain(version)
    source = module.TICKETS["ticket-demo-3"]
    private = source["private_fields"]["synthetic_callback"]
    record = {"ticket": source, "messages": [{"role": "assistant", "content": private.lower()}]}
    safe = module.telemetry_view(record)
    assert safe["ticket"]["private_fields"] == {
        "synthetic_callback": "[REDACTED]", "synthetic_access_note": "[REDACTED]",
    }
    assert safe["messages"] == [{"role": "assistant", "content": "[REDACTED]"}]
    assert private not in json.dumps(safe)
    assert record["ticket"]["private_fields"]["synthetic_callback"] == private


@pytest.mark.parametrize("sentences,reply", [
    ("one sentence", "The fictional ticket is open at revision 2."),
    ("two sentences", "The fictional ticket is open. Its revision is 2."),
])
def test_private_summary_keeps_sentence_format_and_masks_model_echo(sentences, reply):
    module = load_domain("v0")
    private = module.TICKETS["ticket-demo-3"]["private_fields"]["synthetic_callback"]
    text = f"Summarize ticket-demo-3 in {sentences}; do not repeat {private}."
    output, _, model_calls = execute(module, text, model_reply=reply)
    assert output.count(".") == reply.count(".") + 2  # Two dotted field paths, no new sentence.
    assert "=[REDACTED]" in output
    assert private.lower() not in model_calls[0][0].lower()
    output, _, _ = execute(module, "Read ticket-demo-3.", model_reply=private)
    assert private not in output
