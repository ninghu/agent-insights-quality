"""Explicit real Responses/OTel integration; model and network boundaries stay local."""

import asyncio
import importlib
import importlib.util
from importlib.metadata import version as installed_version
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
from azure.ai.agentserver.responses import CreateResponse
from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_to_envelope
import httpx
import pytest
from openai import AsyncOpenAI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent_insights_quality.catalogs import load_catalog


ROOT = Path(__file__).resolve().parents[2] / "agents" / "support-ticket-agent"
VERSIONS = [target.unit_id.logical_version for target in load_catalog(ROOT.parents[1]).for_agent("support-ticket-agent")]


def test_hosting_sdk_matches_deployed_plain_mapping_contract():
    requirement = next(
        line for line in (ROOT / "v0" / "requirements.txt").read_text().splitlines()
        if line.startswith("azure-ai-agentserver-responses==")
    )
    assert installed_version("azure-ai-agentserver-responses") == requirement.split("==")[1]
    payload = CreateResponse(input=[{"role": "user", "content": "Acknowledge."}])
    assert isinstance(payload, dict)


@pytest.fixture
def invoke(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("FOUNDRY_", "AZURE_", "OTEL_", "APPLICATIONINSIGHTS_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "synthetic-support")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://support-model.invalid")
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "synthetic-model")

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("Live network access is forbidden in local hosting tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", forbidden_network)

    class LocalCredential:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get_token(self, scope):
            assert scope == "https://ai.azure.com/.default"
            return SimpleNamespace(token="synthetic-local-token")

    providers = []
    packages = []

    def send(version, text=None, *, body=None, model_reply="Local boundary summary.", headers=None):
        directory = ROOT / "v0" if version == "v0" else ROOT / "issues" / version
        package = "support_hosted_" + uuid4().hex
        packages.append(package)
        spec = importlib.util.spec_from_file_location(
            package, directory / "source" / "__init__.py",
            submodule_search_locations=[str(directory / "source")],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
        monkeypatch.setenv("FOUNDRY_AGENT_VERSION", version)
        app = importlib.import_module(package + ".app")
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        providers.append(provider)
        monkeypatch.setattr(app, "tracer", provider.get_tracer("support-local-hosting"))
        monkeypatch.setattr(app, "DefaultAzureCredential", LocalCredential)
        external_requests = []

        def complete(request):
            submitted = json.loads(request.content)
            external_requests.append(submitted)
            return httpx.Response(200, json={
                "id": "resp_external_model_boundary",
                "object": "response",
                "created_at": 0,
                "model": "synthetic-model",
                "status": "completed",
                "output": [{
                    "id": "msg_synthetic",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text", "text": model_reply, "annotations": [],
                    }],
                }],
            })

        def client(**kwargs):
            assert kwargs["base_url"] == "https://support-model.invalid/openai/v1"
            return AsyncOpenAI(
                **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(complete))
            )

        monkeypatch.setattr(app, "AsyncOpenAI", client)
        request_body = dict(body) if body is not None else {
            "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            "max_output_tokens": 120,
        }
        request_body["store"] = False
        async def post():
            async with app.app.router.lifespan_context(app.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app.app),
                    base_url="http://support-host.invalid",
                ) as local:
                    return await local.post("/responses", json=request_body, headers=headers)

        response = asyncio.run(post())
        assert response.status_code == 200, response.text
        envelope = response.json()
        assert envelope["status"] == "completed", envelope
        output = "".join(
            part["text"] for item in envelope["output"]
            for part in item.get("content", []) if part.get("type") == "output_text"
        )
        spans = exporter.get_finished_spans()
        roots = [span for span in spans if span.attributes.get("gen_ai.operation.name") == "invoke_agent"]
        assert len(roots) == 1
        root = roots[0]
        assert root.attributes["gen_ai.response.id"] == envelope["id"]
        assert envelope["id"] != "resp_external_model_boundary"
        assert root.attributes["gen_ai.agent.name"] == "synthetic-support"
        assert root.attributes["gen_ai.agent.version"] == version
        assert root.attributes["issue.id"] == version
        assert json.loads(root.attributes["gen_ai.input.messages"]) == request_body["input"]
        assert root.attributes["aiq.terminal_response.success"] is True
        assert root.attributes["aiq.terminal_response.output_present"] is True
        assert json.loads(root.attributes["gen_ai.output.messages"])[0]["parts"][0]["content"] == output
        for child in spans:
            assert child.context.trace_id == root.context.trace_id
            if child is not root:
                assert child.parent.span_id == root.context.span_id
            assert child.attributes["gen_ai.agent.version"] == version
        return SimpleNamespace(
            output=output, envelope=envelope, spans=spans, requests=external_requests
        )

    yield send
    for provider in providers:
        provider.shutdown()
    for name in list(sys.modules):
        if any(name == package or name.startswith(package + ".") for package in packages):
            del sys.modules[name]


def operations(result, name):
    return [span for span in result.spans if span.attributes.get("gen_ai.tool.name") == name]


def model_spans(result):
    return [span for span in result.spans if span.attributes.get("gen_ai.operation.name") == "chat"]


def tool_data(span):
    return (
        json.loads(span.attributes["gen_ai.tool.call.arguments"]),
        json.loads(span.attributes["gen_ai.tool.call.result"]),
    )


@pytest.mark.parametrize("version", VERSIONS)
def test_invocation_input_preserves_caller_history_and_deliberate_fault_context(invoke, version):
    inputs = [
        {"role": "user", "content": [{"type": "input_text", "text": "Use ticket-demo-1."}]},
        {"role": "assistant", "content": "Ready."},
        {"role": "user", "content": [{
            "type": "input_text",
            "text": "Read ticket-demo-1 with one temporary read failure; optional history is unavailable.",
        }]},
    ]
    result = invoke(version, body={"input": inputs, "max_output_tokens": 120})
    root = next(span for span in result.spans if span.attributes.get("gen_ai.operation.name") == "invoke_agent")
    assert json.loads(root.attributes["gen_ai.input.messages"]) == inputs
    assert "gen_ai.output.messages" in root.attributes


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("text", [
    "Fixed synthetic case 01. Acknowledge this fixed synthetic conversation context without external action.",
    "I do not confirm update for ticket-demo-2 at revision 1.",
])
def test_setup_and_negation_have_no_tool_or_model_work(invoke, version, text):
    result = invoke(version, text)
    assert len(result.spans) == 1
    assert not result.requests
    assert "external action" in result.output or "not dispatched" in result.output


@pytest.mark.parametrize("ticket_id,revision", [("ticket-demo-1", 3), ("ticket-demo-2", 1)])
def test_baseline_update_executes_and_records_exact_arguments(invoke, ticket_id, revision):
    result = invoke("v0", f"Confirm update for {ticket_id} at revision {revision}.")
    arguments, updated = tool_data(operations(result, "update_ticket")[0])
    assert arguments == {"ticket_id": ticket_id, "expected_revision": revision}
    assert updated["current_revision"] == revision
    assert updated["revision"] == revision + 1
    assert updated["ticket_id"] == ticket_id
    assert f"revision {revision + 1}" in result.output
    assert len(result.requests) == len(model_spans(result)) == 1
    assert model_spans(result)[0].attributes["gen_ai.response.id"] == "resp_external_model_boundary"
    model = model_spans(result)[0]
    assert json.loads(model.attributes["gen_ai.input.messages"])[0]["content"] == result.requests[0]["input"]
    assert json.loads(model.attributes["gen_ai.output.messages"])[0]["content"] == "Local boundary summary."
    assert result.requests[0]["max_output_tokens"] == 120
    assert result.requests[0]["store"] is False


def test_local_paired_recovery_really_dispatches_escalation(invoke):
    text = "Recover ticket-demo-2 after repeated temporary failure."
    healthy, issue = invoke("v0", text), invoke("issue-029", text)
    for result in (healthy, issue):
        assert len(operations(result, "recover_ticket")) == 2
        for span in operations(result, "recover_ticket"):
            arguments, outcome = tool_data(span)
            assert arguments == {"ticket_id": "ticket-demo-2"}
            assert outcome["error"]["code"] == "temporary_unavailable"
            assert span.status.status_code == StatusCode.ERROR
    arguments, outcome = tool_data(operations(healthy, "escalate_ticket")[0])
    assert arguments == {"ticket_id": "ticket-demo-2", "attempts": 2}
    assert outcome["status"] == "escalated"
    assert not operations(issue, "escalate_ticket")
    assert issue.output == "Recovery was exhausted without escalation."


def test_local_paired_stale_update_has_confirmed_mismatched_revision(invoke):
    text = "Confirm update for ticket-demo-2 at revision 0."
    healthy, issue = invoke("v0", text), invoke("issue-030", text)
    assert "rejected" in healthy.output
    assert not operations(healthy, "update_ticket")
    arguments, outcome = tool_data(operations(issue, "update_ticket")[0])
    assert arguments == {"ticket_id": "ticket-demo-2", "expected_revision": 0}
    assert outcome["current_revision"] == 1 and outcome["revision"] == 2
    assert "stale revision 0" in issue.output


def test_local_paired_no_progress_uses_real_poll_results(invoke):
    text = "Wait for ticket-demo-2 while waiting on an unavailable worker."
    for version, count in [("v0", 2), ("issue-031", 4)]:
        result = invoke(version, text)
        polls = operations(result, "poll_ticket")
        assert len(polls) == count
        for index, span in enumerate(polls, 1):
            arguments, outcome = tool_data(span)
            assert arguments == {"ticket_id": "ticket-demo-2"}
            assert outcome["state"] == "waiting" and outcome["revision"] == 1
            assert outcome["poll"] == index
        assert not result.requests


@pytest.mark.parametrize("version,reads", [("issue-032", 0), ("issue-033", 1)])
def test_read_failure_boundary_is_independent_of_endpoint_answer(invoke, version, reads):
    healthy = invoke("v0", "Read ticket-demo-2 and return the result.")
    assert "ticket-demo-2; revision 1; status open" in healthy.output
    issue = invoke(version, "Read ticket-demo-2 and return the result.")
    assert len(operations(issue, "read_ticket")) == reads
    assert not model_spans(issue) and not issue.requests
    if reads:
        _, outcome = tool_data(operations(issue, "read_ticket")[0])
        assert outcome["ok"] is True and outcome["ticket"]["summary"] == "Synthetic app access"
        assert "useful answer" in issue.output
        assert "Synthetic app access" not in issue.output
    else:
        assert "rejected before" in issue.output


def application_guard_facts(result):
    root = next(span for span in result.spans if span.attributes.get("gen_ai.operation.name") == "invoke_agent")
    return {
        key: value for key, value in root.attributes.items()
        if key.startswith("support.application_guard.")
    }


@pytest.mark.parametrize("attempt_index", range(1, 11))
def test_issue032_canonical_guard_facts_are_on_the_response_owned_span(invoke, attempt_index):
    traffic = json.loads((ROOT / "issues" / "issue-032" / "traffic.json").read_text(encoding="utf-8"))
    attempt = traffic["attempts"][attempt_index - 1]
    requests = {row["id"]: row for row in traffic["requests"]}
    setup = invoke("issue-032", body=requests[attempt["setup_steps"][0]]["request"]["body"])
    assert setup.output == "Acknowledged. No external action was taken."
    assert not application_guard_facts(setup) and not setup.requests
    probe = requests[attempt["probe_steps"][0]]
    result = invoke("issue-032", body=probe["request"]["body"])
    assert result.output == "Valid ticket request rejected before model or tool dispatch."
    assert application_guard_facts(result) == {
        "support.application_guard.request_kind": "read",
        "support.application_guard.ticket_known": True,
        "support.application_guard.decision": "reject",
        "support.application_guard.reason": "request_kind_blocked",
        "support.application_guard.short_circuit_stage": "before_business_dispatch",
    }
    assert len(result.spans) == 1
    assert not result.requests and not model_spans(result)
    root = result.spans[0]
    assert root.attributes["gen_ai.response.id"] == result.envelope["id"]
    assert root.attributes["aiq.terminal_response.success"] is True
    assert root.status.status_code == StatusCode.OK
    assert not root.events
    assert "error.type" not in root.attributes and "gen_ai.request.model" not in root.attributes


def test_issue032_summary_guard_is_specific_to_the_current_request_kind(invoke):
    result = invoke("issue-032", "Summarize ticket-demo-2 in one sentence.")
    assert result.output == "Valid ticket request rejected before model or tool dispatch."
    assert application_guard_facts(result)["support.application_guard.request_kind"] == "summarize"
    assert len(result.spans) == 1 and not result.requests


def test_issue032_guard_preserves_w3c_parent_and_response_ownership(invoke):
    trace_id = "0123456789abcdef0123456789abcdef"
    parent_id = "0123456789abcdef"
    result = invoke(
        "issue-032", "Read valid ticket-demo-2.",
        headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
    )
    root = result.spans[0]
    assert root.context.trace_id == int(trace_id, 16)
    assert root.parent.span_id == int(parent_id, 16)
    assert root.attributes["gen_ai.response.id"] == result.envelope["id"]
    assert application_guard_facts(result)["support.application_guard.decision"] == "reject"


def test_issue032_guard_survives_the_installed_exporter_dependency_conversion(invoke):
    result = invoke("issue-032", "Read valid ticket-demo-2.")
    root = result.spans[0]
    # Convert locally only: never construct an exporter or submit telemetry.
    envelope = _convert_span_to_envelope(root)
    assert envelope.name == "Microsoft.ApplicationInsights.RemoteDependency"
    assert envelope.data.base_type == "RemoteDependencyData"
    assert envelope.data.base_data.success is True
    assert envelope.tags["ai.operation.id"] == f"{root.context.trace_id:032x}"
    properties = envelope.data.base_data.properties
    assert properties["gen_ai.response.id"] == result.envelope["id"]
    assert {key: properties[key] for key in application_guard_facts(result)} == {
        key: str(value) for key, value in application_guard_facts(result).items()
    }


@pytest.mark.parametrize("text", [
    "Read a ticket.",
    "Read ticket-demo-1 and ticket-demo-2.",
    "Read ticket-demo-99.",
    "Summarize ticket-demo-99.",
    "Do not confirm update for ticket-demo-1 at revision 3.",
])
def test_issue032_non_guard_returns_do_not_assert_the_guard_decision(invoke, text):
    result = invoke("issue-032", text)
    assert not application_guard_facts(result)
    assert result.output != "Valid ticket request rejected before model or tool dispatch."
    assert len(result.spans) == 1 and not result.requests


@pytest.mark.parametrize("text", [
    "Recover ticket-demo-2 after one temporary read failure.",
    "Recover ticket-demo-2 after repeated temporary failure.",
    "Wait for ticket-demo-2.",
    "Wait for ticket-demo-2 while waiting on an unavailable worker.",
    "Confirm update for ticket-demo-2 at revision 1.",
    "Confirm update for ticket-demo-2 at revision 0.",
    "Confirm update for ticket-demo-2 at revision 1 while preserving shared revision state.",
])
def test_issue032_other_hosted_workflows_are_not_marked_as_guard_short_circuits(invoke, text):
    healthy, issue = invoke("v0", text), invoke("issue-032", text)
    assert issue.output == healthy.output
    assert issue.requests == healthy.requests
    assert not application_guard_facts(issue)
    assert [
        (span.attributes["gen_ai.tool.name"], tool_data(span))
        for span in issue.spans if "gen_ai.tool.name" in span.attributes
    ] == [
        (span.attributes["gen_ai.tool.name"], tool_data(span))
        for span in healthy.spans if "gen_ai.tool.name" in span.attributes
    ]


def test_actual_dispatcher_exception_and_recovery_are_naturally_traced(invoke):
    text = "Read ticket-demo-2 after one deterministic synthetic model failure."
    for version, attempts in [("v0", 2), ("issue-034", 1)]:
        result = invoke(version, text)
        models = model_spans(result)
        assert len(models) == attempts
        assert not result.requests
        assert models[0].status.status_code == StatusCode.ERROR
        assert models[0].attributes["error.type"] == "synthetic_model_failure"
        assert models[0].attributes["gen_ai.request.model"] == "synthetic-ticket-dispatcher"
        assert any(event.name == "exception" and
                   "temporarily unavailable" in event.attributes["exception.message"]
                   for event in models[0].events)
        if attempts == 2:
            assert "recovered after one bounded retry" in result.output
            assert models[1].status.status_code != StatusCode.ERROR
        else:
            assert "without bounded recovery" in result.output


def test_false_success_does_not_fabricate_an_update_span(invoke):
    text = "Confirm update for ticket-demo-2 at revision 1."
    healthy, issue = invoke("v0", text), invoke("issue-035", text)
    assert len(operations(healthy, "update_ticket")) == 1
    assert len(operations(issue, "read_ticket")) == 1
    assert not operations(issue, "update_ticket")
    assert not issue.requests
    assert issue.output == "Update completed successfully."


def test_local_paired_state_loss_has_one_transition_and_two_actual_failures(invoke):
    text = "Confirm update for ticket-demo-2 at revision 1 while preserving shared revision state."
    healthy, issue = invoke("v0", text), invoke("issue-036", text)
    for result in (healthy, issue):
        assert len(operations(result, "propagate_state")) == 1
        before, after = tool_data(operations(result, "propagate_state")[0])
        assert before["state"] == {"ticket_id": "ticket-demo-2", "revision": 1}
        assert after["state"] == (before["state"] if result is healthy else {})
    read_args, read_result = tool_data(operations(issue, "read_ticket")[0])
    update_args, update_result = tool_data(operations(issue, "update_ticket")[0])
    assert read_args == {"ticket_id": None}
    assert read_result["error"]["code"] == "ticket_id_missing"
    assert update_args == {"ticket_id": None, "expected_revision": None}
    assert update_result["error"]["code"] == "revision_missing"
    assert "ticket identifier was lost" in issue.output and "revision was lost" in issue.output
    assert "updated to revision 2 with shared state preserved" in healthy.output


def test_hosted_input_history_does_not_reuse_confirmation(invoke):
    result = invoke("v0", body={"input": [
        {"role": "user", "content": "Confirm update for ticket-demo-2 at revision 1."},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "user", "content": "Read ticket-demo-2."},
    ]})
    assert not operations(result, "update_ticket")
    assert "revision 1" in result.output


def test_baseline_transient_failure_and_partial_result_are_actual_tool_results(invoke):
    recovered = invoke("v0", "Recover ticket-demo-2 after one temporary read failure.")
    reads = operations(recovered, "read_ticket")
    assert len(reads) == 2
    assert tool_data(reads[0])[1]["error"]["code"] == "temporary_unavailable"
    assert tool_data(reads[1])[1]["ticket"]["revision"] == 1
    assert "succeeded after one bounded retry" in recovered.output
    partial = invoke("v0", "Read ticket-demo-2 while its optional history is unavailable.")
    assert tool_data(operations(partial, "read_ticket")[0])[1]["ok"] is True
    assert tool_data(operations(partial, "read_history")[0])[1]["error"]["code"] == "history_unavailable"
    assert "revision 1; status open; summary Synthetic app access; optional history unavailable" in partial.output
    assert not partial.requests


@pytest.mark.parametrize("attempt_index", [2, 7])
def test_canonical_one_sentence_summary_preserves_request_and_complete_model_reply(invoke, attempt_index):
    traffic = json.loads((ROOT / "v0" / "traffic.json").read_text(encoding="utf-8"))
    attempt = next(item for item in traffic["attempts"] if item["index"] == attempt_index)
    probe = next(item for item in traffic["requests"] if item["id"] == attempt["probe_steps"][0])
    reply = "Synthetic ticket-demo-2 is open at revision 1 for app access, with no change dispatched."
    result = invoke("v0", body=probe["request"]["body"], model_reply=reply)
    reads, models = operations(result, "read_ticket"), model_spans(result)
    assert len(reads) == len(models) == len(result.requests) == 1
    arguments, outcome = tool_data(reads[0])
    assert arguments == {"ticket_id": "ticket-demo-2"}
    assert outcome["ok"] is True and outcome["ticket"]["revision"] == 1
    prompt = result.requests[0]["input"]
    assert "summarize ticket-demo-2 in one sentence." in prompt
    assert outcome["ticket"]["summary"] in prompt
    assert "no update was dispatched" in prompt
    assert json.loads(models[0].attributes["gen_ai.input.messages"])[0]["content"] == prompt
    assert json.loads(models[0].attributes["gen_ai.output.messages"])[0]["content"] == reply
    assert result.output == reply
    assert result.output.count(".") == 1
    assert len(result.output.split()) <= probe["expected"]["semantic_assertions"]["max_words"]
    for term in probe["expected"]["semantic_assertions"]["required_terms_all"]:
        assert term in result.output


@pytest.mark.parametrize("version", VERSIONS)
def test_reviewed_probe_goes_through_actual_responses_host(invoke, version):
    directory = ROOT / "v0" if version == "v0" else ROOT / "issues" / version
    traffic = json.loads((directory / "traffic.json").read_text(encoding="utf-8"))
    requests = {item["id"]: item for item in traffic["requests"]}
    probe = requests[traffic["attempts"][0]["probe_steps"][0]]
    result = invoke(version, body=probe["request"]["body"])
    assertions = probe["expected"]["semantic_assertions"]
    if "exact_json" in assertions:
        assert json.loads(result.output) == assertions["exact_json"]
    if "exact_text" in assertions:
        assert result.output == assertions["exact_text"]
    for term in assertions.get("required_terms_all", []):
        assert term.lower() in result.output.lower()


@pytest.mark.parametrize("attempt_index", range(1, 11))
def test_all_canonical_handoffs_have_full_upstream_evidence_and_only_final_omissions(invoke, attempt_index):
    traffic = json.loads((ROOT / "issues" / "issue-007" / "traffic.json").read_text(encoding="utf-8"))
    attempt = traffic["attempts"][attempt_index - 1]
    requests = {row["id"]: row for row in traffic["requests"]}
    probe = requests[attempt["probe_steps"][0]]
    for version in ("v0", "issue-007"):
        setup = invoke(version, body=requests[attempt["setup_steps"][0]]["request"]["body"])
        assert setup.output == "Acknowledged. No external action was taken."
        assert len(setup.spans) == 1 and not setup.requests
        result = invoke(version, body=probe["request"]["body"])
        text = probe["request"]["body"]["input"][0]["content"][0]["text"]
        facts = json.loads(text.split("Handoff facts: ", 1)[1])
        ticket_id = probe["expected"]["semantic_assertions"]["exact_json"]["ticket_id"]
        reads, prepared = operations(result, "read_ticket"), operations(result, "prepare_handoff")
        assert len(reads) == len(prepared) == 1
        assert tool_data(reads[0])[0] == {"ticket_id": ticket_id}
        assert tool_data(reads[0])[1]["ticket"]["status"] == "open"
        arguments, outcome = tool_data(prepared[0])
        assert arguments == {"ticket_id": ticket_id, **facts}
        assert outcome == {"ok": True, "ticket_id": ticket_id, "handoff": facts}
        assert reads[0].end_time <= prepared[0].start_time
        assert json.loads(result.output) == (
            {"ticket_id": ticket_id, **facts} if version == "v0"
            else probe["expected"]["semantic_assertions"]["exact_json"]
        )
        assert len(result.spans) == 3
        assert not model_spans(result) and not result.requests
        assert not operations(result, "update_ticket")
        assert all(span.status.status_code != StatusCode.ERROR for span in result.spans)


@pytest.mark.parametrize("version", VERSIONS)
def test_handoff_is_healthy_in_every_other_version_without_claiming_an_update(invoke, version):
    traffic = json.loads((ROOT / "v0" / "traffic.json").read_text(encoding="utf-8"))
    probe = next(row for row in traffic["requests"] if row["id"].endswith("probe-06-01"))
    result = invoke(version, body=probe["request"]["body"])
    expected = dict(probe["expected"]["semantic_assertions"]["exact_json"])
    if version == "issue-007":
        expected.pop("deadline")
        expected.pop("validation")
    assert json.loads(result.output) == expected
    assert len(result.spans) == 3 and not result.requests and not model_spans(result)
    assert not application_guard_facts(result)
